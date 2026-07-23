from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation.audit import CampaignAudit
from evaluation.outcomes import stable_hash
from evaluation.prompt_campaign import (
    PromptCampaignSpec,
    build_prompt_improvement_brief,
)
from evaluation.prompt_deployment import finalize_prompt_campaign
from runtime.prompt_activation import (
    PromptActivationError,
    activate_prompt_candidate,
    active_prompt_inventory,
    load_active_prompt_state,
    read_active_prompt,
    rollback_active_prompt,
)
from runtime.state import StateStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dataset(path: Path) -> Path:
    path.mkdir()
    record = {
        "schema_version": 2,
        "example_id": "planner-example",
        "run_id": "run-planner-example",
        "role": "planner",
        "program": "PlannerProgram",
        "route": "local-plan",
        "split": "dev",
        "task": "Plan a bounded change.",
        "inputs": {
            "task": "Plan a bounded change.",
            "delegated_task": "Return one atomic step.",
            "repository_evidence": ["sample.py"],
        },
        "output": {
            "instruction": "Change sample.py.",
            "editable_files": ["sample.py"],
            "acceptance_criteria": ["sample.py changed"],
            "depends_on": [],
        },
        "outcome": {
            "verification_passed": True,
            "reviewer_verdict": "pass",
            "reviewer_feedback": "pass",
            "score": 1.0,
        },
    }
    examples = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    examples_path = path / "examples.jsonl"
    examples_path.write_text(examples, encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "dataset_hash": stable_hash([record]),
        "source_run_ids": [record["run_id"]],
        "files": {"examples.jsonl": _sha256(examples_path)},
    }
    manifest["manifest_hash"] = stable_hash(manifest)
    (path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _scorecard(*, promote: bool) -> dict[str, object]:
    names = (
        "safety",
        "correctness",
        "regression",
        "control",
        "improvement",
        "efficiency",
    )
    gates = []
    for name in names:
        passed = promote or name != "regression"
        gates.append({"name": name, "passed": passed, "evidence": {}})
    return {
        "gates": gates,
        "recommendation": (
            "eligible_for_promotion" if promote else "reject_at_regression"
        ),
    }


def _lineage(
    tmp_path: Path,
    *,
    promote: bool,
    close: bool,
) -> tuple[StateStore, str, str, str, Path]:
    dataset = _dataset(tmp_path / "dataset")
    spec = PromptCampaignSpec.from_dataset(
        dataset,
        role="planner",
        reflection_route="local-plan",
        target_metric_calls=8,
        hard_model_call_limit=16,
        max_unsafe_proposals=2,
        no_improvement_patience=2,
        reflection_max_tokens=128,
        max_instruction_chars=800,
        allow_perfect_only=True,
        force_search_perfect_baseline=False,
        seed=0,
        num_threads=1,
    )
    budget = {
        "campaign_wall_seconds": 60,
        "process_wall_seconds": 30,
        "max_processes": 8,
        "max_output_bytes": 100_000,
        "max_memory_mb": 512,
        "max_file_mb": 16,
        "max_disk_mb": 64,
        "max_prompt_tokens": 10_000,
        "max_completion_tokens": 5_000,
        "max_model_calls": 16,
    }
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="baseline-commit",
        suite_hash="suite-hash",
        budget=budget,
        max_candidates=1,
        holdout_hash="holdout-hash",
        environment_hash="environment-hash",
        kind="prompt-optimization",
        prompt_evaluator_hash="evaluator-hash",
    )
    brief = build_prompt_improvement_brief(
        spec,
        baseline_commit="baseline-commit",
        suite_hash="suite-hash",
        budget=budget,
        rollback_condition="Any regression.",
        forbidden_files=(),
        evidence_run_ids=("run-planner-example",),
        evaluation_holdout={
            "mode": "external",
            "identity_hash": "holdout-hash",
        },
        prompt_evaluator_hash="evaluator-hash",
    )
    brief_id = store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief_id,
        actor="tester",
        rationale="fixture",
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id=brief_id,
        overlay_hash=None,
        overlay=None,
        build_kind="prompt-optimization",
    )
    output = tmp_path / "candidate-output"
    output.mkdir()
    candidate = {"predict.predict": "Improved planner instruction."}
    candidate_path = output / "candidate.json"
    candidate_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    candidate_hash = _sha256(candidate_path)
    artifact = {
        "schema_version": 2,
        "artifact_kind": "prompt_candidate",
        "campaign_kind": "prompt-optimization",
        "role": "planner",
        "dataset": str(dataset.resolve()),
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
        "candidate_instruction_hash": stable_hash(candidate),
        "gepa_candidate_hash": candidate_hash,
        "candidate_accepted": True,
        "selected_candidate_changed": True,
        "build_outcome": "candidate_ready",
        "activation": "not_performed",
        "promotion": "not_performed",
        "output": str(output.resolve()),
    }
    store.add_candidate_artifact(
        build_id,
        kind="prompt_candidate",
        path=str(output.resolve()),
        content_hash=stable_hash(artifact),
        content=json.dumps(artifact, sort_keys=True),
    )
    store.complete_candidate_build(
        build_id,
        run_id=None,
        status="candidate_ready",
        branch=None,
        worktree=None,
    )
    evaluation_id = store.create_evaluation(
        campaign_id=campaign_id,
        build_id=build_id,
        baseline_commit="baseline-commit",
        candidate_commit=artifact["candidate_instruction_hash"],
        suite_id="prompt-suite",
        suite_hash="suite-hash",
        holdout_hash="holdout-hash",
        environment_hash="environment-hash",
        repetitions=1,
        budget=budget,
    )
    store.complete_evaluation(
        evaluation_id,
        status="completed",
        scorecard=_scorecard(promote=promote),
    )
    if close:
        store.record_promotion_decision(
            evaluation_id,
            actor="tester",
            decision="promote" if promote else "reject",
            rationale="fixture decision",
        )
        store.close_campaign_from_evidence(campaign_id)
    return store, campaign_id, build_id, evaluation_id, candidate_path


class _LoadRecorder:
    def __init__(self) -> None:
        self.loaded: str | None = None

    def load(self, path: str) -> None:
        self.loaded = path


def test_missing_active_prompt_keeps_code_baseline(tmp_path: Path) -> None:
    program = _LoadRecorder()
    result = load_active_prompt_state(
        program,
        "planner",
        store_root=tmp_path / "prompt-store",
    )
    assert result is None
    assert program.loaded is None


def test_promoted_candidate_activates_and_loads(tmp_path: Path) -> None:
    store, _, _, evaluation_id, candidate_path = _lineage(
        tmp_path,
        promote=True,
        close=True,
    )
    prompt_store = tmp_path / "prompt-store"
    result = activate_prompt_candidate(
        store,
        evaluation_id,
        actor="operator",
        rationale="deploy verified planner",
        repository_root=tmp_path,
        store_root=prompt_store,
    )
    assert result["role"] == "planner"
    assert result["idempotent"] is False
    active = read_active_prompt("planner", store_root=prompt_store)
    assert active is not None
    assert active["program_hash"] == _sha256(candidate_path)

    program = _LoadRecorder()
    loaded = load_active_prompt_state(
        program,
        "planner",
        store_root=prompt_store,
    )
    assert loaded is not None
    assert program.loaded == loaded["resolved_program_state_path"]
    assert Path(program.loaded).read_bytes() == candidate_path.read_bytes()

    repeated = activate_prompt_candidate(
        store,
        evaluation_id,
        actor="operator",
        rationale="idempotent retry",
        repository_root=tmp_path,
        store_root=prompt_store,
    )
    assert repeated["idempotent"] is True
    artifacts = store.evaluation_details(evaluation_id)["artifacts"]
    assert [item["kind"] for item in artifacts].count("prompt_activation") == 1


def test_rejected_candidate_cannot_activate(tmp_path: Path) -> None:
    store, _, _, evaluation_id, _ = _lineage(
        tmp_path,
        promote=False,
        close=True,
    )
    with pytest.raises(PromptActivationError, match="close cleanly"):
        activate_prompt_candidate(
            store,
            evaluation_id,
            actor="operator",
            rationale="must fail",
            repository_root=tmp_path,
            store_root=tmp_path / "prompt-store",
        )


def test_corrupt_active_pointer_fails_closed(tmp_path: Path) -> None:
    prompt_store = tmp_path / "prompt-store"
    pointer = prompt_store / "active" / "planner.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PromptActivationError, match="schema version"):
        read_active_prompt("planner", store_root=prompt_store)


def test_rollback_first_activation_restores_code_baseline(tmp_path: Path) -> None:
    store, _, _, evaluation_id, _ = _lineage(
        tmp_path,
        promote=True,
        close=True,
    )
    prompt_store = tmp_path / "prompt-store"
    activate_prompt_candidate(
        store,
        evaluation_id,
        actor="operator",
        rationale="activate",
        repository_root=tmp_path,
        store_root=prompt_store,
    )
    event = rollback_active_prompt(
        store,
        "planner",
        actor="operator",
        rationale="rollback",
        repository_root=tmp_path,
        store_root=prompt_store,
    )
    assert event["restored"] == "code_baseline"
    assert read_active_prompt("planner", store_root=prompt_store) is None
    artifacts = store.evaluation_details(evaluation_id)["artifacts"]
    assert any(item["kind"] == "prompt_rollback" for item in artifacts)


def test_active_prompt_inventory_is_hash_verified(tmp_path: Path) -> None:
    store, _, _, evaluation_id, _ = _lineage(
        tmp_path,
        promote=True,
        close=True,
    )
    prompt_store = tmp_path / "prompt-store"
    activate_prompt_candidate(
        store,
        evaluation_id,
        actor="operator",
        rationale="activate",
        repository_root=tmp_path,
        store_root=prompt_store,
    )
    inventory = active_prompt_inventory(store_root=prompt_store)
    assert [item["role"] for item in inventory] == ["planner"]


def test_finalizer_derives_rejection_and_closes(tmp_path: Path) -> None:
    store, campaign_id, _, evaluation_id, _ = _lineage(
        tmp_path,
        promote=False,
        close=False,
    )
    audit = CampaignAudit(campaign_id=campaign_id, passed=True, checks=())
    with patch("evaluation.prompt_deployment.audit_campaign", return_value=audit):
        result = finalize_prompt_campaign(
            store,
            campaign_id,
            actor="operator",
            rationale="derive from scorecard",
            activate=False,
            repository_root=tmp_path,
        )
    assert result["decision"] == "reject"
    assert result["status"] == "completed_regression"
    assert store.evaluation_details(evaluation_id)["decision"]["decision"] == "reject"


def test_finalizer_never_activates_a_rejection(tmp_path: Path) -> None:
    store, campaign_id, _, _, _ = _lineage(
        tmp_path,
        promote=False,
        close=False,
    )
    audit = CampaignAudit(campaign_id=campaign_id, passed=True, checks=())
    with patch("evaluation.prompt_deployment.audit_campaign", return_value=audit):
        result = finalize_prompt_campaign(
            store,
            campaign_id,
            actor="operator",
            rationale="derive from scorecard",
            activate=True,
            repository_root=tmp_path,
        )
    assert result["decision"] == "reject"
    assert result["activation"] is None
    assert result["activation_status"] == "skipped_not_eligible"


def test_rollback_restores_previous_authorized_activation(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_store, _, _, first_evaluation, first_candidate = _lineage(
        first_root,
        promote=True,
        close=True,
    )
    second_store, _, _, second_evaluation, second_candidate = _lineage(
        second_root,
        promote=True,
        close=True,
    )
    second_candidate.write_text(
        json.dumps(
            {"predict.predict": "Second improved planner instruction."},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    second_build = second_store.evaluation_details(second_evaluation)["build_id"]
    build = second_store.candidate_build_details(second_build)
    artifact_row = build["artifacts"][0]
    artifact = json.loads(artifact_row["content"])
    artifact["gepa_candidate_hash"] = _sha256(second_candidate)
    artifact["candidate_instruction_hash"] = stable_hash(
        {"predict.predict": "Second improved planner instruction."}
    )
    with second_store.connect() as connection:
        connection.execute(
            """
            UPDATE candidate_artifacts
            SET content = ?, content_hash = ? WHERE id = ?
            """,
            (
                json.dumps(artifact, sort_keys=True),
                stable_hash(artifact),
                artifact_row["id"],
            ),
        )

    prompt_store = tmp_path / "prompt-store"
    first = activate_prompt_candidate(
        first_store,
        first_evaluation,
        actor="operator",
        rationale="first activation",
        repository_root=first_root,
        store_root=prompt_store,
    )
    second = activate_prompt_candidate(
        second_store,
        second_evaluation,
        actor="operator",
        rationale="second activation",
        repository_root=second_root,
        store_root=prompt_store,
    )
    assert second["previous_activation_id"] == first["activation_id"]

    event = rollback_active_prompt(
        second_store,
        "planner",
        actor="operator",
        rationale="restore first",
        repository_root=second_root,
        store_root=prompt_store,
    )
    assert event["restored"] == "previous_activation"
    active = read_active_prompt("planner", store_root=prompt_store)
    assert active is not None
    assert active["activation_id"] == first["activation_id"]
    assert Path(active["resolved_program_state_path"]).read_bytes() == (
        first_candidate.read_bytes()
    )


def test_finalizer_can_activate_an_eligible_campaign(tmp_path: Path) -> None:
    store, campaign_id, _, _, _ = _lineage(
        tmp_path,
        promote=True,
        close=False,
    )
    audit = CampaignAudit(campaign_id=campaign_id, passed=True, checks=())
    prompt_store = tmp_path / "prompt-store"
    with patch("evaluation.prompt_deployment.audit_campaign", return_value=audit):
        result = finalize_prompt_campaign(
            store,
            campaign_id,
            actor="operator",
            rationale="derive and activate",
            activate=True,
            repository_root=tmp_path,
            prompt_store_root=prompt_store,
        )
    assert result["decision"] == "promote"
    assert result["status"] == "completed_clean"
    assert result["activation"]["role"] == "planner"
    assert read_active_prompt("planner", store_root=prompt_store) is not None


def test_rollback_rejects_tampered_previous_state(tmp_path: Path) -> None:
    first_root = tmp_path / "first-tampered"
    second_root = tmp_path / "second-tampered"
    first_root.mkdir()
    second_root.mkdir()
    first_store, _, _, first_evaluation, _ = _lineage(
        first_root,
        promote=True,
        close=True,
    )
    second_store, _, _, second_evaluation, _ = _lineage(
        second_root,
        promote=True,
        close=True,
    )
    prompt_store = tmp_path / "prompt-store-tampered"
    first = activate_prompt_candidate(
        first_store,
        first_evaluation,
        actor="operator",
        rationale="first activation",
        repository_root=first_root,
        store_root=prompt_store,
    )
    second = activate_prompt_candidate(
        second_store,
        second_evaluation,
        actor="operator",
        rationale="second activation",
        repository_root=second_root,
        store_root=prompt_store,
    )
    previous_program = (
        prompt_store / "history" / first["activation_id"] / "program.json"
    )
    previous_program.write_text("{}\n", encoding="utf-8")

    with pytest.raises(PromptActivationError, match="hash does not match"):
        rollback_active_prompt(
            second_store,
            "planner",
            actor="operator",
            rationale="must fail closed",
            repository_root=second_root,
            store_root=prompt_store,
        )
    active = read_active_prompt("planner", store_root=prompt_store)
    assert active is not None
    assert active["activation_id"] == second["activation_id"]
