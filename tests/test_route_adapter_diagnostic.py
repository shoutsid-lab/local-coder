from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.route_adapter_diagnostic import (
    AdapterDiagnosticError,
    assess_planner_prediction,
    assess_reviewer_prediction,
    collect_report,
    compare_reports,
    load_protocol,
    run_attempt,
    validate_report,
)


def _hash_report(report: dict[str, Any]) -> None:
    unhashed = dict(report)
    unhashed.pop("collection_sha256", None)
    encoded = json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
    report["collection_sha256"] = hashlib.sha256(encoded).hexdigest()


def _usage(route: str, prompt: int, completion: int):
    return lambda: {
        f"openai/{route}": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
    }


def _planner_prediction(route: str = "local-plan") -> SimpleNamespace:
    return SimpleNamespace(
        instruction="Change the local-reason timeout to 240 seconds.",
        editable_files=["runtime/route_profiles.py"],
        acceptance_criteria=["local-reason timeout_seconds is 240."],
        depends_on=[],
        get_lm_usage=_usage(route, 100, 40),
    )


def _reviewer_prediction(route: str = "local-review") -> SimpleNamespace:
    return SimpleNamespace(
        verdict="fail",
        summary="runtime/route_profiles.py still uses the old timeout.",
        issues=["runtime/route_profiles.py leaves timeout_seconds at 300."],
        unrelated_changes=[],
        get_lm_usage=_usage(route, 120, 30),
    )


def _identity(subject_name: str) -> dict[str, Any]:
    subject = load_protocol().subjects[subject_name]
    return {
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "build_info": "b-test",
        "configured_context_tokens": 32768,
        "total_slots": 1,
    }


def _report(subject_name: str) -> dict[str, Any]:
    protocol = load_protocol()

    def lm_factory(route: str) -> SimpleNamespace:
        return SimpleNamespace(route=route)

    def planner_runner(*, lm: SimpleNamespace, **_: Any) -> SimpleNamespace:
        return _planner_prediction(lm.route)

    def reviewer_runner(*, lm: SimpleNamespace, **_: Any) -> SimpleNamespace:
        return _reviewer_prediction(lm.route)

    return collect_report(
        protocol=protocol,
        subject_name=subject_name,
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity=_identity(subject_name),
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lm_factory,
        planner_runner=planner_runner,
        reviewer_runner=reviewer_runner,
    )


def test_protocol_freezes_same_program_routes_for_both_subjects() -> None:
    protocol = load_protocol()

    assert protocol.attempts_per_role == 5
    assert protocol.subjects["baseline"].routes == {
        "planner": "local-plan",
        "reviewer": "local-review",
    }
    assert protocol.subjects["candidate"].routes == {
        "planner": "local-reason",
        "reviewer": "local-reason",
    }


def test_prediction_assessment_separates_schema_from_semantics() -> None:
    planner = _planner_prediction()
    planner.editable_files = ["README.md"]
    reviewer = _reviewer_prediction()
    reviewer.verdict = "needs_attention"

    assert assess_planner_prediction(planner) == {
        "schema_valid": True,
        "task_semantics_valid": False,
        "contract_failures": ["task_semantics_mismatch"],
    }
    assert assess_reviewer_prediction(reviewer) == {
        "schema_valid": True,
        "task_semantics_valid": False,
        "contract_failures": ["task_semantics_mismatch"],
    }


def test_run_attempt_classifies_adapter_failure_without_storing_text() -> None:
    def failing_runner(**_: Any) -> Any:
        raise RuntimeError("generated content must not be retained")

    record = run_attempt(
        role="planner",
        index=1,
        route="local-plan",
        lm=object(),
        runner=failing_runner,
    )

    assert record["adapter_success"] is False
    assert record["contract_failures"] == ["adapter_error"]
    assert "generated content" not in json.dumps(record)


def test_collection_runs_both_subjects_through_identical_adapter_entry_points() -> None:
    protocol = load_protocol()
    calls: list[tuple[str, str]] = []

    def lm_factory(route: str) -> SimpleNamespace:
        calls.append(("lm", route))
        return SimpleNamespace(route=route)

    def planner_runner(*, lm: SimpleNamespace, **inputs: Any) -> SimpleNamespace:
        assert set(inputs) == {"task", "delegated_task", "repository_evidence"}
        calls.append(("PlannerProgram/JSONAdapter", lm.route))
        return _planner_prediction(lm.route)

    def reviewer_runner(*, lm: SimpleNamespace, **inputs: Any) -> SimpleNamespace:
        assert set(inputs) == {
            "task",
            "changed_files",
            "verification_passed",
            "verification_output",
            "diff",
        }
        calls.append(("ReviewerProgram/JSONAdapter", lm.route))
        return _reviewer_prediction(lm.route)

    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity=_identity("candidate"),
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lm_factory,
        planner_runner=planner_runner,
        reviewer_runner=reviewer_runner,
    )

    assert calls.count(("PlannerProgram/JSONAdapter", "local-reason")) == 5
    assert calls.count(("ReviewerProgram/JSONAdapter", "local-reason")) == 5
    assert report["summary"]["planner"]["schema_rate"] == 1.0
    assert report["summary"]["reviewer"]["task_semantics_rate"] == 1.0
    serialized = json.dumps(report)
    assert "Change the local-reason timeout" not in serialized
    assert "leaves timeout_seconds" not in serialized


def test_report_validation_rederives_summary_and_profile_binding() -> None:
    protocol = load_protocol()
    report = _report("baseline")
    validate_report(report, protocol)

    tampered = deepcopy(report)
    tampered["attempts"][0]["schema_valid"] = False
    tampered["attempts"][0]["task_semantics_valid"] = False
    tampered["attempts"][0]["contract_failures"] = ["schema_mismatch"]
    _hash_report(tampered)

    with pytest.raises(AdapterDiagnosticError, match="summary"):
        validate_report(tampered, protocol)

    profile_tampered = deepcopy(report)
    profile_tampered["route_profiles"]["planner"]["temperature"] = 1.0
    _hash_report(profile_tampered)

    with pytest.raises(AdapterDiagnosticError, match="protocol"):
        validate_report(profile_tampered, protocol)


def test_comparison_requires_same_commit_and_reports_role_deltas() -> None:
    protocol = load_protocol()
    baseline = _report("baseline")
    candidate = _report("candidate")

    comparison = compare_reports(
        protocol=protocol,
        baseline_value=baseline,
        candidate_value=candidate,
    )

    assert comparison["qualification_claim"] is None
    assert (
        comparison["comparison"]["planner"]["candidate_minus_baseline"]["schema_rate"]
        == 0.0
    )

    candidate["implementation_commit"] = "b" * 40
    _hash_report(candidate)
    with pytest.raises(AdapterDiagnosticError, match="implementation_commit"):
        compare_reports(
            protocol=protocol,
            baseline_value=baseline,
            candidate_value=candidate,
        )


def test_protocol_rejects_route_profile_drift(tmp_path) -> None:
    protocol = json.loads(
        open(
            "profiles/qwythos-f3-adapter-contract-v1.json",
            encoding="utf-8",
        ).read()
    )
    protocol["subjects"]["candidate"]["route_profiles"]["planner"]["temperature"] = 0.0
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(AdapterDiagnosticError, match="does not match local-reason"):
        load_protocol(path)


def test_comparison_rejects_different_active_prompt_lineage() -> None:
    protocol = load_protocol()
    baseline = _report("baseline")
    candidate = _report("candidate")
    candidate["active_prompt_state"]["planner"] = {
        "activation_id": "a1",
        "campaign_id": "c1",
        "build_id": "b1",
        "evaluation_id": "e1",
        "candidate_instruction_hash": "i1",
        "program_hash": "p1",
    }
    _hash_report(candidate)

    with pytest.raises(AdapterDiagnosticError, match="active_prompt_state"):
        compare_reports(
            protocol=protocol,
            baseline_value=baseline,
            candidate_value=candidate,
        )
