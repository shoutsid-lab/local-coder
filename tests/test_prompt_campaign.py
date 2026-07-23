from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from evaluation.outcomes import stable_hash
from evaluation.prompt_campaign import (
    PROMPT_CANDIDATE_ARTIFACT_KIND,
    PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    PromptCampaignError,
    PromptCampaignSpec,
    build_prompt_candidate,
    build_prompt_candidate_artifact,
    build_prompt_improvement_brief,
    prompt_candidate_build_status,
    require_prompt_candidate_evaluation_eligibility,
)
from runtime.migrations import MIGRATIONS
from runtime.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
CLI_SPEC = importlib.util.spec_from_file_location(
    "local_coder_prompt_campaign_cli", ROOT / "local-coder.py"
)
assert CLI_SPEC and CLI_SPEC.loader
CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI)

BASELINE_INSTRUCTION = "Baseline planner instruction."
PROPOSED_INSTRUCTION = "Improved concise planner instruction."
BASELINE_INSTRUCTION_HASH = stable_hash({"predict.predict": BASELINE_INSTRUCTION})
PROPOSED_INSTRUCTION_HASH = stable_hash({"predict.predict": PROPOSED_INSTRUCTION})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dataset(path: Path) -> Path:
    path.mkdir()
    example = {
        "schema_version": 2,
        "example_id": "planner-example",
        "run_id": "run-1",
        "role": "planner",
        "program": "PlannerProgram",
        "route": "local-plan",
        "split": "train",
        "task": "Plan one change.",
        "inputs": {
            "task": "Plan one change.",
            "delegated_task": "Plan it.",
            "repository_evidence": ["a.py"],
        },
        "output": {
            "reasoning": "One file.",
            "instruction": "Change a.py.",
            "editable_files": ["a.py"],
            "acceptance_criteria": ["a.py changed"],
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
        "trace_hash": "trace",
    }
    records = [example]
    payloads = {
        "examples.jsonl": json.dumps(example, sort_keys=True, separators=(",", ":"))
        + "\n",
        "train.jsonl": json.dumps(example, sort_keys=True, separators=(",", ":"))
        + "\n",
        "dev.jsonl": "",
        "holdout.jsonl": "",
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
            "total": 1,
            "by_split": {"train": 1, "dev": 0, "holdout": 0},
            "by_role": {"planner": 1},
        },
        "source_run_ids": ["run-1"],
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


def _fake_gepa_result(
    output: Path,
    dataset_hash: str,
    *,
    outcome: str = "improved_candidate",
    accepted: bool = True,
) -> dict[str, object]:
    output.mkdir(parents=True)
    selected_instruction = PROPOSED_INSTRUCTION if accepted else BASELINE_INSTRUCTION
    candidate = {
        "predict.predict": {"signature": {"instructions": selected_instruction}},
        "metadata": {"dependency_versions": {"dspy": "test"}},
    }
    selected_hash = PROPOSED_INSTRUCTION_HASH if accepted else BASELINE_INSTRUCTION_HASH
    report = {
        "dataset_hash": dataset_hash,
        "role": "planner",
        "optimization": {
            "instruction_hashes": {
                "baseline": BASELINE_INSTRUCTION_HASH,
                "proposed": PROPOSED_INSTRUCTION_HASH,
                "selected": selected_hash,
            },
            "optimization_outcome": outcome,
            "winning_candidate": "optimized" if accepted else "baseline",
            "proposed_candidate_changed": True,
            "selected_candidate_changed": accepted,
            "candidate_changed": accepted,
            "candidate_accepted": accepted,
        },
        "budget": {"effective_target_metric_calls": 20},
        "metric_call_accounting": {"total_observed": 7},
        "model_call_accounting": {"hard_limit": 32, "total": 5},
        "activation": "not_performed",
        "promotion": "not_performed",
    }
    (output / "candidate.json").write_text(
        json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 2,
        "dataset_hash": dataset_hash,
        "role": "planner",
        "dry_run": False,
        "files": {
            "candidate.json": _sha256(output / "candidate.json"),
            "report.json": _sha256(output / "report.json"),
        },
    }
    manifest["manifest_hash"] = stable_hash(manifest)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"manifest": manifest, "report": report}


def test_schema_v9_preserves_v8_campaign_defaults(tmp_path: Path) -> None:
    database = tmp_path / "v8.db"
    with sqlite3.connect(database) as connection:
        for migration in MIGRATIONS[:8]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (migration.version, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.execute("""
            INSERT INTO evaluation_campaigns (
                id, baseline_commit, status, suite_hash, budget, max_candidates,
                created_at, updated_at, holdout_hash, environment_hash
            ) VALUES (
                'campaign', 'base', 'active', 'suite', '{}', 1,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', 'holdout', 'environment'
            )
            """)

    store = StateStore(database)
    details = store.campaign_details("campaign")

    assert details is not None
    assert details["kind"] == "source"
    assert details["candidate_artifacts"] == []
    assert store.schema_version() == 9


def test_prompt_spec_freezes_dataset_and_builds_typed_brief(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)

    brief = build_prompt_improvement_brief(
        spec,
        baseline_commit="abc123",
        suite_hash="suite",
        budget={"max_model_calls": 20},
        rollback_condition="Any holdout regression.",
        forbidden_files=("evaluation/",),
        evidence_run_ids=spec.source_run_ids,
    )

    assert brief["failure_class"] == "prompt_optimization"
    assert brief["allowed_files"] == ()
    assert brief["evidence_run_ids"] == ("run-1",)
    assert brief["metadata"]["campaign_kind"] == PROMPT_OPTIMIZATION_CAMPAIGN_KIND
    assert brief["metadata"]["evaluation_holdout"] == {
        "mode": "unspecified",
        "identity_hash": None,
    }
    assert PromptCampaignSpec.from_metadata(brief["metadata"]) == spec


def test_prompt_spec_rejects_changed_dataset_identity(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    metadata = spec.to_metadata()
    metadata["prompt_optimization"]["dataset_hash"] = "tampered"

    try:
        PromptCampaignSpec.from_metadata(metadata)
    except PromptCampaignError as exc:
        assert "identity changed" in str(exc)
    else:
        raise AssertionError("tampered dataset identity was accepted")


def test_prompt_candidate_artifact_is_inert_and_hash_bound(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"

    def optimizer(dataset_path: Path, output_path: Path, **kwargs):
        del dataset_path, kwargs
        return _fake_gepa_result(output_path, spec.dataset_hash)

    result, artifact = build_prompt_candidate(spec, output, optimizer=optimizer)

    assert result["report"]["activation"] == "not_performed"
    assert artifact["artifact_kind"] == PROMPT_CANDIDATE_ARTIFACT_KIND
    assert artifact["dataset_hash"] == spec.dataset_hash
    assert artifact["baseline_instruction_hash"] == BASELINE_INSTRUCTION_HASH
    assert artifact["candidate_instruction_hash"] == PROPOSED_INSTRUCTION_HASH
    assert artifact["candidate_accepted"] is True
    assert artifact["build_outcome"] == "candidate_ready"
    assert artifact["proposed_candidate_changed"] is True
    assert artifact["selected_candidate_changed"] is True


def test_prompt_candidate_rejects_activation(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(output, spec.dataset_hash)
    result["report"]["activation"] = "performed"
    (output / "report.json").write_text(
        json.dumps(result["report"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result["manifest"]["files"]["report.json"] = _sha256(output / "report.json")
    manifest_input = dict(result["manifest"])
    manifest_input.pop("manifest_hash", None)
    result["manifest"]["manifest_hash"] = stable_hash(manifest_input)
    (output / "manifest.json").write_text(
        json.dumps(result["manifest"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    try:
        build_prompt_candidate_artifact(spec, output, result)
    except PromptCampaignError as exc:
        assert "activation" in str(exc)
    else:
        raise AssertionError("active prompt candidate was accepted")


def test_state_records_prompt_campaign_build_and_artifact(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 20},
        max_candidates=1,
        holdout_hash="holdout",
        environment_hash="environment",
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    brief = {
        "id": "brief",
        "evidence_run_ids": ["run-1"],
        "baseline_commit": "base",
        "failure_class": "prompt_optimization",
        "hypothesis": "Improve planner.",
        "allowed_files": [],
        "forbidden_files": ["evaluation/"],
        "acceptance_metrics": [{"measure": "score"}],
        "suite_hash": "suite",
        "budget": {"max_model_calls": 20},
        "rollback_condition": "Any regression.",
        "metadata": {"campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND},
    }
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        "brief", actor="review-model", rationale="Bounded prompt test."
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id="brief",
        overlay_hash=None,
        overlay=None,
        build_kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    artifact = {"artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND}
    store.add_candidate_artifact(
        build_id,
        kind=PROMPT_CANDIDATE_ARTIFACT_KIND,
        path="/tmp/candidate",
        content_hash=stable_hash(artifact),
        content=json.dumps(artifact),
    )
    store.complete_candidate_build(
        build_id,
        run_id=None,
        status="candidate_ready",
        branch=None,
        worktree=None,
    )

    details = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    assert details is not None and details["kind"] == PROMPT_OPTIMIZATION_CAMPAIGN_KIND
    assert details["candidate_builds"][0]["status"] == "candidate_ready"
    assert details["candidate_artifacts"][0]["kind"] == PROMPT_CANDIDATE_ARTIFACT_KIND
    assert build is not None
    assert build["build_kind"] == PROMPT_OPTIMIZATION_CAMPAIGN_KIND
    assert build["run"] is None
    assert len(build["artifacts"]) == 1


def test_build_candidate_dispatches_prompt_campaign_without_orchestrator(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 20},
        max_candidates=1,
        holdout_hash="holdout",
        environment_hash="environment",
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    brief = build_prompt_improvement_brief(
        spec,
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 20},
        rollback_condition="Any regression.",
        forbidden_files=("evaluation/",),
        evidence_run_ids=spec.source_run_ids,
    )
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief["id"], actor="review-model", rationale="Run bounded GEPA."
    )
    artifact = {
        "schema_version": 1,
        "artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND,
        "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        "role": "planner",
        "build_outcome": "candidate_ready",
    }
    result = {"manifest": {"manifest_hash": "manifest"}, "report": {}}
    args = Namespace(
        campaign_id=campaign_id,
        database=store.path,
        output=tmp_path / "output",
        max_steps=12,
        overlay=None,
    )

    with patch(
        "evaluation.prompt_campaign.build_prompt_candidate",
        return_value=(result, artifact),
    ):
        status = CLI.handle_build_candidate(args)

    assert status == 0
    details = store.campaign_details(campaign_id)
    assert details is not None
    assert details["candidate_builds"][0]["status"] == "candidate_ready"
    assert details["candidate_builds"][0]["run_id"] is None
    assert json.loads(details["candidate_artifacts"][0]["content"]) == artifact


def test_prompt_campaign_parser_allows_deferred_holdout(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    args = CLI.build_parser().parse_args(
        [
            "create-campaign",
            "--kind",
            "prompt-optimization",
            "--baseline",
            ".",
            "--dataset",
            str(dataset),
            "--role",
            "planner",
            "--rollback-condition",
            "Any regression.",
        ]
    )

    assert args.holdout_suite is None
    assert args.holdout_oracle is None


def test_prompt_campaign_freezes_deferred_holdout_identity(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    args = Namespace(
        development_suite=ROOT / "evaluation" / "suites" / "atomic-v1.json",
        holdout_suite=None,
        holdout_oracle=None,
    )

    suite_hash, holdout_hash, metadata = CLI._load_campaign_evaluation_identity(
        args,
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        prompt_spec=spec,
    )

    assert len(suite_hash) == 64
    assert holdout_hash is None
    assert metadata == {
        "mode": "deferred",
        "identity_hash": None,
        "required_before": "paired_prompt_evaluation",
    }


def test_source_campaign_still_requires_external_holdout(tmp_path: Path) -> None:
    args = Namespace(
        development_suite=ROOT / "evaluation" / "suites" / "atomic-v1.json",
        holdout_suite=None,
        holdout_oracle=None,
    )

    try:
        CLI._load_campaign_evaluation_identity(args, kind="source")
    except ValueError as exc:
        assert "Source campaigns require" in str(exc)
    else:
        raise AssertionError("source campaign accepted a deferred holdout")


def test_campaign_rejects_partial_holdout_arguments(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    args = Namespace(
        development_suite=ROOT / "evaluation" / "suites" / "atomic-v1.json",
        holdout_suite=tmp_path / "manifest.json",
        holdout_oracle=None,
    )

    try:
        CLI._load_campaign_evaluation_identity(
            args,
            kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
            prompt_spec=spec,
        )
    except ValueError as exc:
        assert "both --holdout-suite and --holdout-oracle" in str(exc)
    else:
        raise AssertionError("partial holdout arguments were accepted")


def test_prompt_candidate_rejection_describes_selected_baseline(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(
        output,
        spec.dataset_hash,
        outcome="rejected_unsafe_candidate",
        accepted=False,
    )

    artifact = build_prompt_candidate_artifact(spec, output, result)

    assert artifact["proposed_candidate_changed"] is True
    assert artifact["selected_candidate_changed"] is False
    assert artifact["candidate_changed"] is False
    assert artifact["candidate_instruction_hash"] == BASELINE_INSTRUCTION_HASH
    assert artifact["build_outcome"] == "candidate_rejected"
    assert prompt_candidate_build_status(artifact) == "candidate_rejected"


def test_prompt_candidate_null_result_is_not_evaluation_ready(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(
        output,
        spec.dataset_hash,
        outcome="no_improvement",
        accepted=False,
    )
    artifact = build_prompt_candidate_artifact(spec, output, result)
    build = {
        "status": prompt_candidate_build_status(artifact),
        "artifacts": [{"content": json.dumps(artifact)}],
    }

    assert artifact["build_outcome"] == "no_improvement"
    try:
        require_prompt_candidate_evaluation_eligibility(build)
    except PromptCampaignError as exc:
        assert "not evaluation-ready" in str(exc)
    else:
        raise AssertionError("null-result candidate entered paired evaluation")


def test_only_accepted_changed_prompt_is_evaluation_ready(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(output, spec.dataset_hash)
    artifact = build_prompt_candidate_artifact(spec, output, result)
    build = {
        "status": "candidate_ready",
        "artifacts": [{"content": json.dumps(artifact)}],
    }

    require_prompt_candidate_evaluation_eligibility(build)


def test_prompt_spec_reads_schema_v1_with_bounded_defaults(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    metadata = spec.to_metadata()
    payload = metadata["prompt_optimization"]
    payload["schema_version"] = 1
    payload["max_metric_calls"] = payload.pop("target_metric_calls")
    payload.pop("hard_model_call_limit")
    payload.pop("max_unsafe_proposals")

    migrated = PromptCampaignSpec.from_metadata(metadata)

    assert migrated.schema_version == 2
    assert migrated.hard_model_call_limit == migrated.target_metric_calls
    assert migrated.max_unsafe_proposals == 3


def test_state_accepts_terminal_prompt_build_outcomes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    for index, status in enumerate(("candidate_rejected", "no_improvement")):
        campaign_id = store.create_campaign(
            baseline_commit="base",
            suite_hash=f"suite-{index}",
            budget={"max_model_calls": 20},
            max_candidates=1,
            holdout_hash=None,
            environment_hash="environment",
            kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        )
        brief_id = f"brief-{index}"
        brief = {
            "id": brief_id,
            "evidence_run_ids": [],
            "baseline_commit": "base",
            "failure_class": "prompt_optimization",
            "hypothesis": "Test terminal prompt state.",
            "allowed_files": [],
            "forbidden_files": [],
            "acceptance_metrics": [],
            "suite_hash": f"suite-{index}",
            "budget": {"max_model_calls": 20},
            "rollback_condition": "Any regression.",
            "metadata": {"campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND},
        }
        store.add_improvement_brief(campaign_id, brief)
        store.approve_improvement_brief(
            brief_id, actor="review-model", rationale="Bounded."
        )
        build_id = store.create_candidate_build(
            campaign_id,
            brief_id=brief_id,
            overlay_hash=None,
            overlay=None,
            build_kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        )
        store.complete_candidate_build(
            build_id,
            run_id=None,
            status=status,
            branch=None,
            worktree=None,
        )
        assert store.candidate_build_details(build_id)["status"] == status


def test_build_candidate_persists_rejected_prompt_status(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 32},
        max_candidates=1,
        holdout_hash=None,
        environment_hash="environment",
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    brief = build_prompt_improvement_brief(
        spec,
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 32},
        rollback_condition="Any regression.",
        forbidden_files=("evaluation/",),
        evidence_run_ids=spec.source_run_ids,
    )
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief["id"], actor="review-model", rationale="Run bounded GEPA."
    )
    artifact = {
        "schema_version": 2,
        "artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND,
        "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        "role": "planner",
        "build_outcome": "candidate_rejected",
    }
    args = Namespace(
        campaign_id=campaign_id,
        database=store.path,
        output=tmp_path / "output",
        max_steps=12,
        overlay=None,
    )

    with patch(
        "evaluation.prompt_campaign.build_prompt_candidate",
        return_value=({"manifest": {}, "report": {}}, artifact),
    ):
        status = CLI.handle_build_candidate(args)

    assert status == 0
    details = store.campaign_details(campaign_id)
    assert details is not None
    assert details["candidate_builds"][0]["status"] == "candidate_rejected"


def test_rejected_prompt_build_is_blocked_before_holdout_loading(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"max_model_calls": 32},
        max_candidates=1,
        holdout_hash=None,
        environment_hash="environment",
        kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    brief = {
        "id": "brief",
        "evidence_run_ids": [],
        "baseline_commit": "base",
        "failure_class": "prompt_optimization",
        "hypothesis": "Test rejected evaluation boundary.",
        "allowed_files": [],
        "forbidden_files": [],
        "acceptance_metrics": [],
        "suite_hash": "suite",
        "budget": {"max_model_calls": 32},
        "rollback_condition": "Any regression.",
        "metadata": {"campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND},
    }
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief("brief", actor="review-model", rationale="Bounded.")
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id="brief",
        overlay_hash=None,
        overlay=None,
        build_kind=PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
    )
    store.complete_candidate_build(
        build_id,
        run_id=None,
        status="candidate_rejected",
        branch=None,
        worktree=None,
    )
    args = Namespace(
        campaign_id=campaign_id,
        build_id=build_id,
        database=store.path,
    )

    assert CLI.handle_evaluate(args) == 1


def _rewrite_gepa_result(
    output: Path,
    result: dict[str, object],
) -> None:
    report = result["report"]
    manifest = result["manifest"]
    assert isinstance(report, dict)
    assert isinstance(manifest, dict)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = manifest["files"]
    assert isinstance(files, dict)
    files["candidate.json"] = _sha256(output / "candidate.json")
    files["report.json"] = _sha256(output / "report.json")
    manifest_input = dict(manifest)
    manifest_input.pop("manifest_hash", None)
    manifest["manifest_hash"] = stable_hash(manifest_input)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_prompt_candidate_rejects_contradictory_change_flags(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(output, spec.dataset_hash)
    report = result["report"]
    assert isinstance(report, dict)
    optimization = report["optimization"]
    assert isinstance(optimization, dict)
    optimization["candidate_changed"] = False
    _rewrite_gepa_result(output, result)

    try:
        build_prompt_candidate_artifact(spec, output, result)
    except PromptCampaignError as exc:
        assert "candidate change flag" in str(exc)
    else:
        raise AssertionError("contradictory prompt lineage was accepted")


def test_prompt_candidate_rejects_saved_instruction_mismatch(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dataset")
    spec = _spec(dataset)
    output = tmp_path / "candidate"
    result = _fake_gepa_result(output, spec.dataset_hash)
    candidate = json.loads((output / "candidate.json").read_text(encoding="utf-8"))
    candidate["predict.predict"]["signature"]["instructions"] = "Tampered."
    (output / "candidate.json").write_text(
        json.dumps(candidate, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _rewrite_gepa_result(output, result)

    try:
        build_prompt_candidate_artifact(spec, output, result)
    except PromptCampaignError as exc:
        assert "selected instruction hash" in str(exc)
    else:
        raise AssertionError("candidate file with inconsistent lineage was accepted")
