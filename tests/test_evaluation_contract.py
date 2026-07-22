from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation.manifests import load_suite
from evaluation.scorecard import build_scorecard
from evaluation.supervisor import (
    CaseResult,
    EvaluationBudget,
    PairedEvaluation,
    ProcessResult,
    Supervisor,
)
from runtime.editor import EditorError, load_editable_files

ROOT = Path(__file__).resolve().parents[1]


def _process(returncode: int = 0) -> ProcessResult:
    return ProcessResult(
        command=("trusted",),
        returncode=returncode,
        timed_out=False,
        duration_ms=1,
        stdout="{}",
        stderr="",
        output_truncated=False,
    )


def _result(generation: str, passed: bool) -> CaseResult:
    return CaseResult(
        generation=generation,
        repetition=1,
        case_id="target",
        visibility="holdout",
        process=_process(),
        observation_hash="hash",
        oracle_passed=passed,
        policy_passed=True,
        failure=None if passed else "oracle",
    )


def test_recursive_improvement_controls_are_agent_protected(tmp_path: Path) -> None:
    target = tmp_path / "evaluation" / "supervisor.py"
    target.parent.mkdir()
    target.write_text("trusted = True\n", encoding="utf-8")

    try:
        load_editable_files(tmp_path, ["evaluation/supervisor.py"])
    except EditorError as exc:
        assert "Protected file" in str(exc)
    else:
        raise AssertionError("Trusted evaluator was agent-editable")


def test_holdout_manifest_contains_no_oracle_answers() -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout manifest is not mounted into candidate verification.")
    manifest = load_suite(
        ROOT / "evaluation" / "holdout" / "atomic-holdout-v1.json",
        expected_visibility="holdout",
    )

    assert manifest.cases
    assert all(case.oracle is None for case in manifest.cases)


def test_candidate_cannot_replace_base_owned_contract_worker() -> None:
    manifest = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    supervisor = Supervisor(ROOT, EvaluationBudget())
    fake_result = ProcessResult(
        command=("bwrap",),
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout='{"changed": ["sample.txt"], "content": "after\\n"}',
        stderr="",
        output_truncated=False,
    )

    with patch.object(supervisor, "_run", return_value=fake_result) as run:
        supervisor.run_contract(
            ROOT,
            manifest.cases[0],
            generation="candidate",
            repetition=1,
            visibility="development",
            oracle=manifest.cases[0].oracle,
        )

    command = run.call_args.args[0]
    worker_index = command.index("/trusted/contract_worker.py")
    assert command[worker_index - 1] == str(ROOT / "evaluation" / "contract_worker.py")
    assert str(ROOT / "evaluation" / "oracles") not in command
    assert "/candidate/runtime" in command
    assert "/candidate/evaluation" not in command


def test_committed_candidate_cannot_allowlist_trusted_controls() -> None:
    supervisor = Supervisor(ROOT, EvaluationBudget())

    with patch(
        "evaluation.supervisor.generation_changed_paths",
        return_value=("evaluation/supervisor.py",),
    ):
        result = supervisor.run_path_policy(
            ROOT,
            baseline_commit="baseline",
            allowed_paths={"evaluation/supervisor.py"},
            generation="candidate",
            repetition=1,
        )

    assert result.policy_passed is False
    assert result.oracle_passed is False
    assert result.failure == "policy"


def test_scorecard_can_only_recommend_human_promotion() -> None:
    evaluation = PairedEvaluation(
        baseline_commit="base",
        candidate_commit="candidate",
        development_suite_hash="development",
        holdout_suite_hash="holdout",
        holdout_oracle_hash="oracle",
        environment_hash="environment",
        candidate_patch_hash="patch",
        repetitions=1,
        budget=EvaluationBudget(),
        results=(_result("baseline", False), _result("candidate", True)),
    )

    scorecard = build_scorecard(evaluation, target_case_ids=["target"])

    assert scorecard.recommendation == "eligible_for_human_promotion"
    assert scorecard.gates[-1].name == "authority"
    assert scorecard.gates[-1].passed is None
    assert "human" in scorecard.gates[-1].evidence["required"]
