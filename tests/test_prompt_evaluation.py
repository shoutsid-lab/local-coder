from __future__ import annotations

import hashlib
import importlib.util
import json
from argparse import Namespace
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation.audit import audit_campaign
from evaluation.outcomes import stable_hash
from evaluation.prompt_campaign import (
    PROMPT_CANDIDATE_ARTIFACT_KIND,
    PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    PromptCampaignError,
    PromptCampaignSpec,
    build_prompt_improvement_brief,
    load_prompt_candidate_artifact,
    prompt_candidate_content,
)
from evaluation.manifests import load_suite
from evaluation.prompt_deployment import finalize_prompt_campaign
from evaluation.prompt_evaluator import (
    PromptEvaluationError,
    build_prompt_scorecard,
    evaluate_prompt_pair,
    load_prompt_holdout,
    prompt_evaluator_identity,
    prompt_holdout_identity,
)
from evaluation.scorecard import Gate, PromotionScorecard
from evaluation.supervisor import EvaluationBudget, environment_identity
from runtime.prompt_activation import activate_prompt_candidate
from runtime.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
CLI_SPEC = importlib.util.spec_from_file_location(
    "local_coder_prompt_evaluation_cli", ROOT / "local-coder.py"
)
assert CLI_SPEC and CLI_SPEC.loader
CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI)
BASELINE_HASH = stable_hash({"predict.predict": "Baseline planner instruction."})
CANDIDATE_HASH = stable_hash({"predict.predict": "Improved planner instruction."})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _planner_record(example_id: str, split: str, task: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "example_id": example_id,
        "run_id": f"run-{example_id}",
        "role": "planner",
        "program": "PlannerProgram",
        "route": "local-plan",
        "split": split,
        "task": task,
        "inputs": {
            "task": task,
            "delegated_task": "Plan one bounded change.",
            "repository_evidence": ["sample.py"],
        },
        "output": {
            "instruction": "Change sample.py.",
            "editable_files": ["sample.py"],
            "acceptance_criteria": ["sample.py changed"],
            "depends_on": [],
        },
        "outcome": {
            "run_status": "awaiting_approval",
            "verification_passed": True,
            "verification_output": "Verification: PASS",
            "verification_evidence": {},
            "reviewer_verdict": "pass",
            "reviewer_feedback": "pass",
            "score": 1.0,
        },
        "normalized_outcome": {},
        "trace_hash": f"trace-{example_id}",
    }


def _dataset(path: Path) -> Path:
    path.mkdir()
    records = [
        _planner_record("train-example", "train", "Train planner."),
        _planner_record("dev-example", "dev", "Evaluate planner."),
    ]
    split_records = {
        "train": [records[0]],
        "dev": [records[1]],
        "holdout": [],
    }
    payloads = {
        "examples.jsonl": "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        **{
            f"{split}.jsonl": "".join(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                for record in selected
            )
            for split, selected in split_records.items()
        },
    }
    for name, payload in payloads.items():
        (path / name).write_text(payload, encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "trace_schema_version": 1,
        "source_schema_version": 9,
        "dataset_hash": stable_hash(records),
        "split_policy": "test",
        "counts": {
            "total": 2,
            "by_split": {"train": 1, "dev": 1, "holdout": 0},
            "by_role": {"planner": 2},
        },
        "source_run_ids": ["run-train-example", "run-dev-example"],
        "exclusions": {},
        "files": {name: _sha256(path / name) for name in payloads},
    }
    manifest["manifest_hash"] = stable_hash(manifest)
    (path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _spec(dataset: Path) -> PromptCampaignSpec:
    return PromptCampaignSpec.from_dataset(
        dataset,
        role="planner",
        reflection_route="local-plan",
        target_metric_calls=20,
        hard_model_call_limit=32,
        max_unsafe_proposals=3,
        no_improvement_patience=3,
        reflection_max_tokens=256,
        max_instruction_chars=800,
        allow_perfect_only=True,
        force_search_perfect_baseline=False,
        seed=0,
        num_threads=1,
    )


def _holdout(path: Path) -> tuple[Path, Path]:
    path.mkdir()
    manifest = {
        "schema_version": 1,
        "suite_kind": "prompt-replay",
        "suite_id": "planner-external-v1",
        "visibility": "holdout",
        "role": "planner",
        "cases": [
            {
                "id": "external-plan",
                "inputs": {
                    "task": "Plan an unseen change.",
                    "delegated_task": "Plan one bounded change.",
                    "repository_evidence": ["external.py"],
                },
            }
        ],
    }
    oracle = {
        "schema_version": 1,
        "suite_kind": "prompt-replay",
        "suite_id": "planner-external-v1",
        "role": "planner",
        "cases": {
            "external-plan": {
                "output": {
                    "instruction": "Change external.py.",
                    "editable_files": ["external.py"],
                    "acceptance_criteria": ["external.py changed"],
                    "depends_on": [],
                }
            }
        },
    }
    manifest_path = path / "manifest.json"
    oracle_path = path / "oracle.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
    return manifest_path, oracle_path


def _campaign(
    tmp_path: Path,
) -> tuple[
    StateStore,
    str,
    str,
    PromptCampaignSpec,
    dict[str, object],
    EvaluationBudget,
]:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    budget = EvaluationBudget(max_model_calls=16)
    budget_payload = asdict(budget)
    _, environment_hash = environment_identity(ROOT)
    _, prompt_evaluator_hash = prompt_evaluator_identity(ROOT, "planner")
    development = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    suite_hash = stable_hash(
        {
            "development": development.manifest_hash,
            "prompt_dataset": spec.dataset_hash,
            "prompt_dataset_manifest": spec.dataset_manifest_hash,
        }
    )
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="baseline-commit",
        suite_hash=suite_hash,
        budget=budget_payload,
        max_candidates=1,
        holdout_hash=None,
        environment_hash=environment_hash,
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        prompt_evaluator_hash=prompt_evaluator_hash,
    )
    brief = build_prompt_improvement_brief(
        spec,
        baseline_commit="baseline-commit",
        suite_hash=suite_hash,
        budget=budget_payload,
        rollback_condition="Any regression.",
        forbidden_files=("evaluation/",),
        evidence_run_ids=spec.source_run_ids,
        prompt_evaluator_hash=prompt_evaluator_hash,
    )
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief["id"], actor="review-model", rationale="Bounded prompt replay."
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id=brief["id"],
        overlay_hash=None,
        overlay=None,
        build_kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    output = tmp_path / "candidate"
    output.mkdir()
    candidate_state = {
        "predict.predict": {
            "signature": {"instructions": "Improved planner instruction."}
        }
    }
    candidate_path = output / "candidate.json"
    candidate_path.write_text(
        json.dumps(candidate_state, sort_keys=True) + "\n", encoding="utf-8"
    )
    artifact = {
        "schema_version": 2,
        "artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND,
        "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        "role": "planner",
        "dataset": str(dataset),
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
        "baseline_instruction_hash": BASELINE_HASH,
        "candidate_instruction_hash": CANDIDATE_HASH,
        "proposed_instruction_hash": CANDIDATE_HASH,
        "gepa_manifest_hash": "manifest-hash",
        "gepa_candidate_hash": _sha256(candidate_path),
        "gepa_report_hash": "report-hash",
        "optimization_outcome": "improved_candidate",
        "build_outcome": "candidate_ready",
        "winning_candidate": "optimized",
        "proposed_candidate_changed": True,
        "selected_candidate_changed": True,
        "candidate_changed": True,
        "candidate_accepted": True,
        "budget": {"hard_model_call_limit": 16},
        "metric_call_accounting": {"total_observed": 4},
        "model_call_accounting": {
            "hard_limit": 16,
            "student": 3,
            "reflection": 1,
            "total": 4,
            "at_limit": False,
            "blocked_calls": 0,
            "provider_retries_included": False,
        },
        "activation": "not_performed",
        "promotion": "not_performed",
        "output": str(output),
    }
    content = prompt_candidate_content(artifact)
    store.add_candidate_artifact(
        build_id,
        kind=PROMPT_CANDIDATE_ARTIFACT_KIND,
        path=str(output),
        content_hash=stable_hash(artifact),
        content=content,
    )
    store.complete_candidate_build(
        build_id,
        run_id=None,
        status="candidate_ready",
        branch=None,
        worktree=None,
    )
    return store, campaign_id, build_id, spec, artifact, budget


def test_prompt_holdout_keeps_inputs_separate_from_oracle(tmp_path: Path) -> None:
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")

    suite, oracle, oracle_hash = load_prompt_holdout(
        manifest_path, oracle_path, expected_role="planner"
    )

    assert suite.role == "planner"
    assert suite.cases[0].case_id == "external-plan"
    assert "output" not in suite.cases[0].inputs
    assert oracle["external-plan"]["instruction"] == "Change external.py."
    assert prompt_holdout_identity(suite, oracle_hash)


def test_prompt_holdout_rejects_role_mismatch(tmp_path: Path) -> None:
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")

    with pytest.raises(PromptEvaluationError, match="role differs"):
        load_prompt_holdout(manifest_path, oracle_path, expected_role="reviewer")


def test_prompt_candidate_artifact_requires_a_valid_hash(tmp_path: Path) -> None:
    store, _, build_id, _, _, _ = _campaign(tmp_path)
    build = store.candidate_build_details(build_id)
    assert build is not None

    build["artifacts"][0]["content_hash"] = None
    with pytest.raises(PromptCampaignError, match="hash is missing"):
        load_prompt_candidate_artifact(build)

    build = store.candidate_build_details(build_id)
    assert build is not None
    build["artifacts"][0]["content_hash"] = "tampered"
    with pytest.raises(PromptCampaignError, match="hash is invalid"):
        load_prompt_candidate_artifact(build)


def test_prompt_evaluation_rejects_changed_candidate_state(tmp_path: Path) -> None:
    store, campaign_id, build_id, spec, _, budget = _campaign(tmp_path)
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")
    holdout, oracle, oracle_hash = load_prompt_holdout(
        manifest_path, oracle_path, expected_role="planner"
    )
    campaign = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    assert campaign is not None and build is not None
    artifact = load_prompt_candidate_artifact(build)
    candidate_path = Path(str(artifact["output"])) / "candidate.json"
    candidate_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(PromptEvaluationError, match="state hash changed"):
        evaluate_prompt_pair(
            trusted_root=ROOT,
            campaign=campaign,
            build=build,
            spec=spec,
            holdout=holdout,
            holdout_oracle=oracle,
            holdout_oracle_hash=oracle_hash,
            repetitions=1,
            budget=budget,
            state=store,
            campaign_id=campaign_id,
            build_id=build_id,
            prediction_runner=lambda _generation, _inputs: {},
            instruction_hash_loader=lambda _role, _path: (
                BASELINE_HASH,
                CANDIDATE_HASH,
            ),
        )

    assert store.campaign_details(campaign_id)["evaluations"] == []


def test_prompt_scorecard_rejects_external_holdout_regression(
    tmp_path: Path,
) -> None:
    store, campaign_id, build_id, spec, _, budget = _campaign(tmp_path)
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")
    holdout, oracle, oracle_hash = load_prompt_holdout(
        manifest_path, oracle_path, expected_role="planner"
    )
    campaign = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    assert campaign is not None and build is not None

    def predict(generation: str, inputs: dict[str, object]) -> dict[str, object]:
        path = str(inputs["repository_evidence"][0])
        exact = {
            "instruction": f"Change {path}.",
            "editable_files": [path],
            "acceptance_criteria": [f"{path} changed"],
            "depends_on": [],
        }
        if path == "external.py":
            if generation == "baseline":
                return exact
            return {
                **exact,
                "instruction": "Inspect the repository.",
            }
        if generation == "candidate":
            return exact
        return {
            **exact,
            "instruction": "Inspect the repository.",
        }

    _, scorecard = evaluate_prompt_pair(
        trusted_root=ROOT,
        campaign=campaign,
        build=build,
        spec=spec,
        holdout=holdout,
        holdout_oracle=oracle,
        holdout_oracle_hash=oracle_hash,
        repetitions=1,
        budget=budget,
        state=store,
        campaign_id=campaign_id,
        build_id=build_id,
        prediction_runner=predict,
        accounting=lambda: {
            "hard_limit": 16,
            "baseline": 2,
            "candidate": 2,
            "total": 4,
            "at_limit": False,
            "blocked_calls": 0,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_token_limit": budget.max_prompt_tokens,
            "completion_token_limit": budget.max_completion_tokens,
            "provider_retries_included": False,
        },
        instruction_hash_loader=lambda _role, _path: (
            BASELINE_HASH,
            CANDIDATE_HASH,
        ),
    )

    assert scorecard.recommendation == "reject_at_regression"
    assert scorecard.gates[2].passed is False


def test_prompt_evaluation_records_unexpected_runtime_failure(
    tmp_path: Path,
) -> None:
    store, campaign_id, build_id, spec, _, budget = _campaign(tmp_path)
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")
    holdout, oracle, oracle_hash = load_prompt_holdout(
        manifest_path, oracle_path, expected_role="planner"
    )
    campaign = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    assert campaign is not None and build is not None

    def predict(_generation: str, inputs: dict[str, object]) -> dict[str, object]:
        path = str(inputs["repository_evidence"][0])
        return {
            "instruction": f"Change {path}.",
            "editable_files": [path],
            "acceptance_criteria": [f"{path} changed"],
            "depends_on": [],
        }

    def broken_accounting() -> dict[str, object]:
        raise RuntimeError("accounting unavailable")

    with pytest.raises(PromptEvaluationError, match="accounting unavailable"):
        evaluate_prompt_pair(
            trusted_root=ROOT,
            campaign=campaign,
            build=build,
            spec=spec,
            holdout=holdout,
            holdout_oracle=oracle,
            holdout_oracle_hash=oracle_hash,
            repetitions=1,
            budget=budget,
            state=store,
            campaign_id=campaign_id,
            build_id=build_id,
            prediction_runner=predict,
            accounting=broken_accounting,
            instruction_hash_loader=lambda _role, _path: (
                BASELINE_HASH,
                CANDIDATE_HASH,
            ),
        )

    evaluations = store.campaign_details(campaign_id)["evaluations"]
    assert len(evaluations) == 1
    assert evaluations[0]["status"] == "failed"


def test_paired_prompt_evaluation_records_standard_scorecard(
    tmp_path: Path,
) -> None:
    store, campaign_id, build_id, spec, artifact, budget = _campaign(tmp_path)
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")
    holdout, oracle, oracle_hash = load_prompt_holdout(
        manifest_path, oracle_path, expected_role="planner"
    )
    campaign = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    assert campaign is not None and build is not None

    def predict(generation: str, inputs: dict[str, object]) -> dict[str, object]:
        path = str(inputs["repository_evidence"][0])
        instruction = f"Change {path}."
        if generation == "baseline":
            instruction = "Inspect the repository."
        return {
            "instruction": instruction,
            "editable_files": [path],
            "acceptance_criteria": [f"{path} changed"],
            "depends_on": [],
        }

    evaluation, scorecard = evaluate_prompt_pair(
        trusted_root=ROOT,
        campaign=campaign,
        build=build,
        spec=spec,
        holdout=holdout,
        holdout_oracle=oracle,
        holdout_oracle_hash=oracle_hash,
        repetitions=1,
        budget=budget,
        state=store,
        campaign_id=campaign_id,
        build_id=build_id,
        prediction_runner=predict,
        accounting=lambda: {
            "hard_limit": 16,
            "baseline": 2,
            "candidate": 2,
            "total": 4,
            "at_limit": False,
            "blocked_calls": 0,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_token_limit": budget.max_prompt_tokens,
            "completion_token_limit": budget.max_completion_tokens,
            "provider_retries_included": False,
        },
        instruction_hash_loader=lambda _role, _path: (
            BASELINE_HASH,
            CANDIDATE_HASH,
        ),
    )

    assert scorecard.recommendation == "eligible_for_promotion"
    invalid_accounting = dict(evaluation.model_call_accounting)
    invalid_accounting.pop("prompt_tokens")
    invalid_scorecard = build_prompt_scorecard(
        replace(evaluation, model_call_accounting=invalid_accounting),
        artifact=artifact,
    )
    assert invalid_scorecard.recommendation == "reject_at_control"
    assert "invalid_prompt_tokens" in invalid_scorecard.gates[3].evidence["failures"]
    invalid_build_accounting = dict(artifact["model_call_accounting"])
    invalid_build_accounting.pop("student")
    invalid_artifact = {
        **artifact,
        "model_call_accounting": invalid_build_accounting,
    }
    invalid_build_scorecard = build_prompt_scorecard(
        evaluation, artifact=invalid_artifact
    )
    assert invalid_build_scorecard.recommendation == "reject_at_control"
    assert (
        "invalid_candidate_build_student"
        in invalid_build_scorecard.gates[3].evidence["failures"]
    )
    assert evaluation.aggregate("candidate", "development") > evaluation.aggregate(
        "baseline", "development"
    )
    assert evaluation.aggregate("candidate", "holdout") > evaluation.aggregate(
        "baseline", "holdout"
    )
    details = store.evaluation_details(evaluation.evaluation_id)
    assert details is not None
    assert {artifact["kind"] for artifact in details["artifacts"]} == {
        "prompt_program_state",
        "prompt_evaluation_identity",
    }
    assert store.campaign_details(campaign_id)["holdout_hash"]
    redacted = evaluation.to_dict(redact_holdout=True)
    holdout_results = [
        result for result in redacted["results"] if result["visibility"] == "holdout"
    ]
    assert all(result["score"] == "<redacted>" for result in holdout_results)
    assert all(result["observation_hash"] == "<redacted>" for result in holdout_results)

    finalization = finalize_prompt_campaign(
        store,
        campaign_id,
        actor="review-model",
        rationale="All prompt gates passed.",
        activate=False,
        repository_root=ROOT,
    )
    assert finalization["decision"] == "promote"
    assert finalization["status"] == "completed_clean"
    assert finalization["audit"]["passed"] is True
    activation = activate_prompt_candidate(
        store,
        evaluation.evaluation_id,
        actor="review-model",
        rationale="Deploy the promoted prompt.",
        repository_root=ROOT,
        store_root=tmp_path / "prompt-store",
    )
    assert activation["role"] == "planner"
    assert audit_campaign(StateStore(store.path, read_only=True), campaign_id).passed

    with store.connect() as connection:
        connection.execute(
            """
            UPDATE candidate_artifacts
            SET content_hash = 'tampered' WHERE build_id = ?
            """,
            (build_id,),
        )
    tampered_audit = audit_campaign(StateStore(store.path, read_only=True), campaign_id)
    assert tampered_audit.passed is False
    candidate_check = next(
        check
        for check in tampered_audit.checks
        if check.name == "candidate_artifact_lineage"
    )
    assert candidate_check.passed is False


def test_deferred_prompt_holdout_binds_once(tmp_path: Path) -> None:
    store, campaign_id, _, _, _, _ = _campaign(tmp_path)

    store.bind_prompt_campaign_holdout(campaign_id, "holdout-a")
    store.bind_prompt_campaign_holdout(campaign_id, "holdout-a")

    with pytest.raises(ValueError, match="already frozen"):
        store.bind_prompt_campaign_holdout(campaign_id, "holdout-b")


def test_deferred_prompt_evaluator_binds_once(tmp_path: Path) -> None:
    store, campaign_id, _, _, _, _ = _campaign(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE evaluation_campaigns
            SET prompt_evaluator_hash = NULL WHERE id = ?
            """,
            (campaign_id,),
        )

    store.bind_prompt_campaign_evaluator(campaign_id, "evaluator-a")
    store.bind_prompt_campaign_evaluator(campaign_id, "evaluator-a")

    with pytest.raises(ValueError, match="already frozen"):
        store.bind_prompt_campaign_evaluator(campaign_id, "evaluator-b")


def test_evaluate_command_dispatches_prompt_campaign_without_source_paths(
    tmp_path: Path,
    capsys,
) -> None:
    store, campaign_id, build_id, _, _, budget = _campaign(tmp_path)
    manifest_path, oracle_path = _holdout(tmp_path / "holdout")

    class FakeEvaluation:
        def to_dict(self, *, redact_holdout: bool = True) -> dict[str, object]:
            assert redact_holdout is True
            return {
                "artifact_kind": "prompt_evaluation",
                "evaluation_id": "evaluation-1",
            }

    scorecard = PromotionScorecard(
        gates=(
            Gate("safety", True, {}),
            Gate("correctness", True, {}),
            Gate("regression", True, {}),
            Gate("control", True, {}),
            Gate("improvement", True, {}),
            Gate("efficiency", True, {}),
        ),
        recommendation="eligible_for_promotion",
    )
    args = Namespace(
        campaign_id=campaign_id,
        build_id=build_id,
        database=store.path,
        baseline=None,
        candidate=None,
        allowed_file=None,
        target_case=None,
        development_suite=ROOT / "evaluation" / "suites" / "atomic-v1.json",
        holdout_suite=manifest_path,
        holdout_oracle=oracle_path,
        repetitions=1,
        expected_environment_hash=None,
        campaign_seconds=budget.campaign_wall_seconds,
        process_seconds=budget.process_wall_seconds,
        max_processes=budget.max_processes,
        max_output_bytes=budget.max_output_bytes,
        max_memory_mb=budget.max_memory_mb,
        max_file_mb=budget.max_file_mb,
        max_disk_mb=budget.max_disk_mb,
        max_prompt_tokens=budget.max_prompt_tokens,
        max_completion_tokens=budget.max_completion_tokens,
        max_model_calls=budget.max_model_calls,
    )

    with patch(
        "evaluation.prompt_evaluator.evaluate_prompt_pair",
        return_value=(FakeEvaluation(), scorecard),
    ):
        status = CLI.handle_evaluate(args)

    captured = capsys.readouterr()
    assert status == 0
    payload = json.loads(captured.out)
    assert payload["artifact_kind"] == "prompt_evaluation"
    assert payload["scorecard"]["recommendation"] == "eligible_for_promotion"


def test_evaluate_parser_allows_prompt_campaign_without_source_arguments() -> None:
    args = CLI.build_parser().parse_args(
        [
            "evaluate",
            "--campaign-id",
            "campaign",
            "--build-id",
            "build",
            "--holdout-suite",
            "/tmp/manifest.json",
            "--holdout-oracle",
            "/tmp/oracle.json",
        ]
    )

    assert args.baseline is None
    assert args.candidate is None
    assert args.target_case is None


def test_rotate_holdout_accepts_prompt_replay_schema(
    tmp_path: Path,
    capsys,
) -> None:
    manifest_path, oracle_path = _holdout(tmp_path / "source")
    storage = tmp_path / "trusted"
    args = Namespace(
        rotation_id="planner-external-v1",
        manifest=manifest_path,
        oracle=oracle_path,
    )

    with patch.object(CLI, "HOLDOUT_STORAGE", storage):
        status = CLI.handle_rotate_holdout(args)

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["suite_kind"] == "prompt-replay"
    assert Path(payload["holdout_suite"]).is_file()
    assert Path(payload["holdout_oracle"]).is_file()
