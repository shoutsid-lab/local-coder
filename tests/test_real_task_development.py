from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from evaluation.outcomes import stable_hash
from evaluation.real_task_development import (
    PLANNER_DIMENSIONS,
    REVIEWER_DIMENSIONS,
    RealTaskDevelopmentError,
    assess_planner_prediction,
    assess_reviewer_prediction,
    collect_report,
    load_development_suite,
    load_protocol,
    run_case,
    validate_report,
)


class Prediction(SimpleNamespace):
    def __init__(self, *, route: str, **fields: object) -> None:
        super().__init__(**fields)
        self._route = route

    def get_lm_usage(self) -> dict[str, dict[str, int]]:
        return {
            f"openai/{self._route}": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
            }
        }


def _planner_prediction(case: object, route: str = "local-plan") -> Prediction:
    oracle = case.oracle
    return Prediction(
        route=route,
        instruction="Apply " + " and ".join(oracle["required_instruction_terms"]),
        editable_files=list(oracle["editable_files"]),
        acceptance_criteria=[
            "Confirm " + " and ".join(oracle["required_acceptance_terms"])
        ],
        depends_on=list(oracle["depends_on"]),
    )


def _reviewer_prediction(case: object, route: str = "local-review") -> Prediction:
    oracle = case.oracle
    return Prediction(
        route=route,
        verdict=oracle["verdict"],
        summary="The diff and deterministic evidence support this verdict.",
        issues=[f"{path}: definite defect" for path in oracle["required_issue_paths"]],
        unrelated_changes=[
            f"{path}: material unrelated change"
            for path in oracle["required_unrelated_paths"]
        ],
    )


def _service_identity(protocol: object, subject_name: str) -> dict[str, object]:
    subject = protocol.subjects[subject_name]
    return {
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "build_info": "test-build",
        "configured_context_tokens": 32768,
        "total_slots": 1,
    }


def _collect(
    subject_name: str = "baseline",
) -> tuple[object, object, dict[str, object]]:
    protocol = load_protocol()
    suite = load_development_suite(protocol)
    cases_by_task = {case.task: case for case in suite.cases}
    subject = protocol.subjects[subject_name]

    def planner_runner(*, lm: object, **inputs: object) -> Prediction:
        del lm
        case = cases_by_task[inputs["task"]]
        return _planner_prediction(case, subject.routes["planner"])

    def reviewer_runner(*, lm: object, **inputs: object) -> Prediction:
        del lm
        case = cases_by_task[inputs["task"]]
        return _reviewer_prediction(case, subject.routes["reviewer"])

    report = collect_report(
        protocol=protocol,
        suite=suite,
        subject_name=subject_name,
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity=_service_identity(protocol, subject_name),
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lambda route: route,
        planner_runner=planner_runner,
        reviewer_runner=reviewer_runner,
    )
    return protocol, suite, report


def test_protocol_binds_frozen_development_suite_and_routes() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)

    assert suite.suite_id == "real-task-development-v1"
    assert suite.suite_hash == protocol.suite_sha256
    assert len(suite.cases) == 8
    assert protocol.attempts_per_case == 1
    assert protocol.subjects["baseline"].routes == {
        "planner": "local-plan",
        "reviewer": "local-review",
    }
    assert protocol.subjects["candidate"].routes == {
        "planner": "local-reason",
        "reviewer": "local-reason",
    }


def test_planner_assessment_scores_all_frozen_dimensions() -> None:
    protocol = load_protocol()
    case = next(
        case
        for case in load_development_suite(protocol).cases
        if case.role == "planner"
    )

    passed = assess_planner_prediction(_planner_prediction(case), case.oracle)
    wrong_scope = _planner_prediction(case)
    wrong_scope.editable_files = ["runtime/route_profiles.py"]
    failed = assess_planner_prediction(wrong_scope, case.oracle)

    assert tuple(passed["dimensions"]) == PLANNER_DIMENSIONS
    assert passed["case_success"] is True
    assert passed["score"] == 1.0
    assert failed["case_success"] is False
    assert failed["dimensions"]["scope_match"] is False
    assert failed["failure_codes"] == ["scope_mismatch"]


def test_reviewer_assessment_detects_false_positive_paths() -> None:
    protocol = load_protocol()
    case = next(
        case
        for case in load_development_suite(protocol).cases
        if case.role == "reviewer" and case.oracle["forbidden_issue_paths"]
    )

    passed = assess_reviewer_prediction(_reviewer_prediction(case), case.oracle)
    false_positive = _reviewer_prediction(case)
    false_positive.issues = [
        f"{case.oracle['forbidden_issue_paths'][0]}: invented defect"
    ]
    failed = assess_reviewer_prediction(false_positive, case.oracle)

    assert tuple(passed["dimensions"]) == REVIEWER_DIMENSIONS
    assert passed["case_success"] is True
    assert failed["case_success"] is False
    assert failed["dimensions"]["forbidden_issue_paths_absent"] is False
    assert failed["failure_codes"] == ["forbidden_issue_path_reported"]


def test_run_case_classifies_adapter_failure_without_generated_text() -> None:
    protocol = load_protocol()
    case = load_development_suite(protocol).cases[0]

    def broken_runner(**_: object) -> object:
        raise RuntimeError("provider response contained sensitive text")

    record = run_case(
        case=case,
        attempt=1,
        route="local-plan",
        lm=object(),
        runner=broken_runner,
        clock=iter((10.0, 12.5)).__next__,
    )

    assert record["adapter_success"] is False
    assert record["failure_codes"] == ["adapter_error"]
    assert record["latency_seconds"] == 2.5
    assert "sensitive" not in str(record)


def test_collection_scores_every_distinct_development_case() -> None:
    protocol, suite, report = _collect()

    validated = validate_report(report, protocol=protocol, suite=suite)

    assert len(validated["attempts"]) == 8
    assert {item["case_id"] for item in validated["attempts"]} == {
        case.case_id for case in suite.cases
    }
    assert validated["summary"]["overall"]["case_success_rate"] == 1.0
    assert validated["summary"]["by_role"]["planner"]["cases"] == 4
    assert validated["summary"]["by_role"]["reviewer"]["cases"] == 4
    assert validated["holdout_loaded"] is False


def test_report_validation_rejects_rehashed_attempt_tamper() -> None:
    protocol, suite, report = _collect()
    tampered = deepcopy(report)
    tampered["attempts"][0]["dimensions"]["scope_match"] = False
    tampered["collection_sha256"] = stable_hash(
        {key: value for key, value in tampered.items() if key != "collection_sha256"}
    )

    with pytest.raises(RealTaskDevelopmentError, match="inconsistent"):
        validate_report(tampered, protocol=protocol, suite=suite)


def test_report_validation_rejects_rehashed_summary_tamper() -> None:
    protocol, suite, report = _collect()
    tampered = deepcopy(report)
    tampered["summary"]["overall"]["mean_score"] = 0.0
    tampered["collection_sha256"] = stable_hash(
        {key: value for key, value in tampered.items() if key != "collection_sha256"}
    )

    with pytest.raises(RealTaskDevelopmentError, match="summary does not match"):
        validate_report(tampered, protocol=protocol, suite=suite)


def test_candidate_collection_uses_same_cases_and_scoring() -> None:
    protocol, suite, report = _collect("candidate")

    validated = validate_report(report, protocol=protocol, suite=suite)

    assert validated["subject"] == "candidate"
    assert validated["routes"] == {
        "planner": "local-reason",
        "reviewer": "local-reason",
    }
    assert validated["suite_sha256"] == protocol.suite_sha256
    assert validated["scoring_version"] == "role-oracle-v1"
