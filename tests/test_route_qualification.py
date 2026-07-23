from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from runtime.route_profiles import REASONING_PROFILE_EXAMPLES
from runtime.route_qualification import (
    DEFAULT_POLICY_PATH,
    QualificationError,
    evaluate_qualification,
    load_policy,
    main,
)


def contract_run(suite: str, index: int) -> dict[str, object]:
    thinking_enabled = suite != "exact"
    return {
        "attempt_id": f"{suite}-{index}",
        "suite": suite,
        "thinking_enabled": thinking_enabled,
        "response_outcome": "route_ok",
        "final_answer_present": True,
        "schema_valid": True,
        "reasoning_present": thinking_enabled,
        "malformed_tool_call": False,
        "prompt_tokens": 120,
        "completion_tokens": 32 if suite == "exact" else 240,
        "reasoning_tokens": 120 if thinking_enabled else 0,
        "latency_seconds": 12.0,
        "generated_tokens_per_second": 12.0,
    }


def role_case(role: str, split: str, index: int) -> dict[str, object]:
    return {
        "case_id": f"{role}-{split}-{index}",
        "role": role,
        "split": split,
        "baseline_score": 0.75,
        "candidate_score": 0.8,
        "baseline_success": True,
        "candidate_success": True,
        "baseline_repair_iterations": 1,
        "candidate_repair_iterations": 1,
        "out_of_scope_files": 0,
        "response_outcome": "route_ok",
        "final_answer_present": True,
        "schema_valid": True,
        "reasoning_present": True,
        "malformed_tool_call": False,
        "prompt_tokens": 600,
        "completion_tokens": 700,
        "reasoning_tokens": 350,
        "latency_seconds": 45.0,
        "generated_tokens_per_second": 10.0,
    }


def passing_evidence() -> dict[str, object]:
    policy = load_policy()
    contract_runs = [
        contract_run(suite, index)
        for suite in ("exact", "planner", "reviewer")
        for index in range(5)
    ]
    role_cases = [
        role_case(role, split, index)
        for role in ("planner", "reviewer")
        for split, count in (("development", 4), ("holdout", 2))
        for index in range(count)
    ]
    return {
        "schema_version": 1,
        "policy_id": policy.policy_id,
        "policy_sha256": policy.sha256,
        "candidate_model": policy.candidate_model,
        "candidate_route": policy.candidate_route,
        "candidate_profiles": {
            role: dict(profile) for role, profile in policy.expected_profiles.items()
        },
        "baseline_routes": dict(policy.baseline_routes),
        "implementation_commit": "a" * 40,
        "corpus_id": "real-task-v1",
        "corpus_sha256": "b" * 64,
        "environment_id": "amelia-gtx1660-v1",
        "contract_runs": contract_runs,
        "role_cases": role_cases,
        "resources": {
            "startup_seconds": 50.0,
            "model_switch_seconds": 70.0,
            "peak_vram_mib": 5200.0,
            "peak_system_memory_mib": 6400.0,
            "context_tokens_tested": 8192,
        },
    }


def test_frozen_policy_matches_qwythos_route_profile() -> None:
    policy = load_policy(DEFAULT_POLICY_PATH)

    assert policy.policy_id == "qwythos-f3-v1"
    assert policy.candidate_route == "local-reason"
    assert policy.expected_profiles == {
        "planner": {
            "reasoning_mode": "on",
            "max_tokens": 2048,
            "reasoning_tokens": 1024,
            "final_answer_tokens": 1024,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "repetition_penalty": 1.05,
            "timeout_seconds": 300,
            "retries": 0,
        },
        "reviewer": {
            "reasoning_mode": "on",
            "max_tokens": 1536,
            "reasoning_tokens": 768,
            "final_answer_tokens": 768,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "repetition_penalty": 1.05,
            "timeout_seconds": 300,
            "retries": 0,
        },
    }
    assert len(policy.sha256) == 64

    for role in ("planner", "reviewer"):
        profile = REASONING_PROFILE_EXAMPLES[role]
        assert policy.expected_profiles[role] == {
            "reasoning_mode": profile.reasoning_mode,
            "max_tokens": profile.max_tokens,
            "reasoning_tokens": profile.reasoning_tokens,
            "final_answer_tokens": profile.final_answer_tokens,
            "temperature": profile.temperature,
            "top_p": profile.top_p,
            "top_k": profile.top_k,
            "repetition_penalty": profile.repetition_penalty,
            "timeout_seconds": profile.timeout_seconds,
            "retries": profile.retries,
        }


def test_complete_passing_evidence_qualifies_both_roles() -> None:
    decision = evaluate_qualification(passing_evidence(), load_policy())

    assert decision.outcome == "qualified_for_both"
    assert decision.planner_qualified is True
    assert decision.reviewer_qualified is True
    assert decision.global_failures == ()
    assert decision.planner_failures == ()
    assert decision.reviewer_failures == ()
    assert decision.metrics["contract"]["exact"]["reasoning_presence_rate"] == 0
    assert decision.metrics["contract"]["planner"]["reasoning_presence_rate"] == 1


def test_missing_provider_reasoning_token_breakdown_is_allowed() -> None:
    evidence = passing_evidence()
    for record in [*evidence["contract_runs"], *evidence["role_cases"]]:
        record["reasoning_tokens"] = None

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "qualified_for_both"


def test_reviewer_regression_allows_planner_only_outcome() -> None:
    evidence = passing_evidence()
    reviewer_case = next(
        case
        for case in evidence["role_cases"]
        if case["role"] == "reviewer" and case["split"] == "holdout"
    )
    reviewer_case["candidate_score"] = 0.4
    reviewer_case["candidate_success"] = False

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "qualified_for_planner_only"
    assert decision.planner_qualified is True
    assert decision.reviewer_qualified is False
    assert any(
        "material case regressions" in item for item in decision.reviewer_failures
    )
    assert any("baseline successes lost" in item for item in decision.reviewer_failures)


def test_role_contract_failure_prevents_only_that_role() -> None:
    evidence = passing_evidence()
    planner_case = next(
        case for case in evidence["role_cases"] if case["role"] == "planner"
    )
    planner_case["schema_valid"] = False

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "qualified_for_reviewer_only"
    assert decision.planner_qualified is False
    assert decision.reviewer_qualified is True
    assert any(
        "invalid final/schema contract" in item for item in decision.planner_failures
    )


def test_contract_completion_budget_is_a_global_gate() -> None:
    evidence = passing_evidence()
    exact_run = next(
        run for run in evidence["contract_runs"] if run["suite"] == "exact"
    )
    exact_run["completion_tokens"] = 65

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "rejected"
    assert any(
        "completion budget exceeded" in item for item in decision.global_failures
    )


def test_reviewer_role_budget_failure_preserves_planner_decision() -> None:
    evidence = passing_evidence()
    reviewer_case = next(
        case for case in evidence["role_cases"] if case["role"] == "reviewer"
    )
    reviewer_case["completion_tokens"] = 1537

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "qualified_for_planner_only"
    assert any(
        "completion budget exceeded" in item for item in decision.reviewer_failures
    )


def test_role_case_requires_observable_reasoning() -> None:
    evidence = passing_evidence()
    planner_case = next(
        case for case in evidence["role_cases"] if case["role"] == "planner"
    )
    planner_case["reasoning_present"] = False

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "qualified_for_reviewer_only"
    assert any(
        "invalid final/schema contract" in item for item in decision.planner_failures
    )


def test_global_resource_failure_rejects_both_roles() -> None:
    evidence = passing_evidence()
    evidence["resources"]["peak_vram_mib"] = 6000.0

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "rejected"
    assert decision.planner_qualified is False
    assert decision.reviewer_qualified is False
    assert "resources: peak VRAM exceeds the frozen limit" in decision.global_failures


def test_contract_reasoning_only_truncation_rejects_candidate_globally() -> None:
    evidence = passing_evidence()
    planner_probe = next(
        run for run in evidence["contract_runs"] if run["suite"] == "planner"
    )
    planner_probe["response_outcome"] = "reasoning_only_truncated"
    planner_probe["final_answer_present"] = False

    decision = evaluate_qualification(evidence, load_policy())

    assert decision.outcome == "rejected"
    assert any("were not route_ok" in item for item in decision.global_failures)
    assert any("final-answer rate" in item for item in decision.global_failures)


def test_unknown_response_classification_is_rejected() -> None:
    evidence = passing_evidence()
    evidence["contract_runs"][0]["response_outcome"] = "mystery_failure"

    with pytest.raises(QualificationError, match="response_outcome is unsupported"):
        evaluate_qualification(evidence, load_policy())


def test_evidence_must_bind_to_exact_policy_hash() -> None:
    evidence = passing_evidence()
    evidence["policy_sha256"] = "0" * 64

    with pytest.raises(QualificationError, match="policy_sha256"):
        evaluate_qualification(evidence, load_policy())


def test_duplicate_case_identity_is_rejected() -> None:
    evidence = passing_evidence()
    evidence["role_cases"].append(deepcopy(evidence["role_cases"][0]))

    with pytest.raises(QualificationError, match="Duplicate role case identity"):
        evaluate_qualification(evidence, load_policy())


def test_cli_can_print_policy_hash(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--print-policy-hash"]) == 0

    output = capsys.readouterr().out.strip()
    assert output == load_policy().sha256


def test_cli_enforces_requested_role(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = passing_evidence()
    planner_case = next(
        case for case in evidence["role_cases"] if case["role"] == "planner"
    )
    planner_case["schema_valid"] = False
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    assert main([str(evidence_path), "--require", "planner"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "qualified_for_reviewer_only"
