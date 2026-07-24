from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from evaluation.outcomes import stable_hash
from evaluation.real_task_profile_tuning import (
    ProfileTuningError,
    collect_report,
    compare_reports,
    TuningProtocol,
    load_development_suite,
    load_protocol,
    parser,
    validate_report,
)
from runtime.dspy_lm import build_dspy_lm_with_profile


class Prediction(SimpleNamespace):
    def get_lm_usage(self) -> dict[str, dict[str, int]]:
        return {
            "openai/local-reason": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
            }
        }


def _planner_prediction(case: object) -> Prediction:
    oracle = case.oracle
    return Prediction(
        instruction="Apply " + " and ".join(oracle["required_instruction_terms"]),
        editable_files=list(oracle["editable_files"]),
        acceptance_criteria=[
            "Confirm " + " and ".join(oracle["required_acceptance_terms"])
        ],
        depends_on=list(oracle["depends_on"]),
    )


def _reviewer_prediction(case: object) -> Prediction:
    oracle = case.oracle
    return Prediction(
        verdict=oracle["verdict"],
        summary="The bounded diff and verification support this verdict.",
        issues=[f"{path}: definite defect" for path in oracle["required_issue_paths"]],
        unrelated_changes=[
            f"{path}: material unrelated change"
            for path in oracle["required_unrelated_paths"]
        ],
    )


def _service_identity(protocol: object) -> dict[str, object]:
    return {
        "model_file": protocol.candidate_model_file,
        "llama_alias": protocol.llama_alias,
        "build_info": "test-build",
        "configured_context_tokens": 32768,
        "total_slots": 1,
    }


def _profile_id(protocol: object, profile: object, role: str) -> str:
    for profile_id, profiles in protocol.profiles.items():
        if profiles[role] == profile:
            return profile_id
    raise AssertionError("profile not found")


def _collect(
    profile_id: str,
    *,
    scenario: str = "accuracy",
) -> tuple[object, object, dict[str, object]]:
    protocol = load_protocol()
    suite = load_development_suite(protocol)
    cases_by_task = {case.task: case for case in suite.cases}

    def lm_factory(route: str, profile: object) -> SimpleNamespace:
        assert route == "local-reason"
        role = (
            "planner"
            if profile == protocol.profiles[profile_id]["planner"]
            else "reviewer"
        )
        return SimpleNamespace(
            profile_id=_profile_id(protocol, profile, role),
            role=role,
        )

    def planner_runner(*, lm: object, **inputs: object) -> Prediction:
        case = cases_by_task[inputs["task"]]
        prediction = _planner_prediction(case)
        planner_cases = [item for item in suite.cases if item.role == "planner"]
        index = planner_cases.index(case)
        if scenario == "accuracy":
            if lm.profile_id == "current-control" and index in {0, 1}:
                prediction.acceptance_criteria = ["Confirm unrelated behavior"]
            if lm.profile_id == "role-depth-accuracy" and index == 0:
                prediction.acceptance_criteria = ["Confirm unrelated behavior"]
        elif scenario == "regression":
            if lm.profile_id == "current-control" and index in {0, 1, 2}:
                prediction.acceptance_criteria = ["Confirm unrelated behavior"]
            if lm.profile_id == "deterministic-accuracy" and index == 3:
                prediction.editable_files = ["runtime/route_profiles.py"]
                prediction.acceptance_criteria = ["Confirm unrelated behavior"]
            if lm.profile_id == "role-depth-accuracy" and index in {0, 1, 2}:
                prediction.acceptance_criteria = ["Confirm unrelated behavior"]
        return prediction

    def reviewer_runner(*, lm: object, **inputs: object) -> Prediction:
        case = cases_by_task[inputs["task"]]
        prediction = _reviewer_prediction(case)
        reviewer_cases = [item for item in suite.cases if item.role == "reviewer"]
        index = reviewer_cases.index(case)
        if lm.profile_id == "current-control" and index == 0:
            prediction.verdict = "needs_attention"
        if lm.profile_id == "role-depth-accuracy" and index == 1:
            prediction.verdict = "needs_attention"
        return prediction

    report = collect_report(
        protocol=protocol,
        suite=suite,
        profile_id=profile_id,
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity=_service_identity(protocol),
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lm_factory,
        planner_runner=planner_runner,
        reviewer_runner=reviewer_runner,
    )
    return protocol, suite, report


def _all_reports(
    scenario: str = "accuracy",
) -> tuple[object, object, list[dict[str, object]]]:
    reports: list[dict[str, object]] = []
    protocol = suite = None
    for profile_id in load_protocol().profiles:
        protocol, suite, report = _collect(profile_id, scenario=scenario)
        reports.append(report)
    assert protocol is not None and suite is not None
    return protocol, suite, reports


def test_protocol_freezes_three_accuracy_first_profiles() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)

    assert protocol.attempts_per_case == 2
    assert suite.suite_hash == protocol.suite_sha256
    assert set(protocol.profiles) == {
        "current-control",
        "deterministic-accuracy",
        "role-depth-accuracy",
    }
    assert protocol.profiles["deterministic-accuracy"]["planner"].temperature == 0.0
    assert protocol.profiles["role-depth-accuracy"]["planner"].reasoning_tokens == 2048
    assert protocol.selection_policy.control_profile == "current-control"


def test_protocol_rejects_control_profile_drift() -> None:
    raw = deepcopy(load_protocol().raw)
    raw["profiles"]["current-control"]["planner"]["temperature"] = 0.5

    with pytest.raises(ProfileTuningError, match="must match the active"):
        TuningProtocol.from_mapping(raw)


def test_dspy_lm_factory_accepts_explicit_bounded_profile() -> None:
    protocol = load_protocol()
    profile = protocol.profiles["role-depth-accuracy"]["planner"]
    calls: list[tuple[str, dict[str, object]]] = []

    def lm(model: str, **kwargs: object) -> SimpleNamespace:
        calls.append((model, kwargs))
        return SimpleNamespace(model=model)

    result = build_dspy_lm_with_profile(
        "local-reason",
        profile,
        dspy_module=SimpleNamespace(LM=lm),
    )

    assert result.model == "openai/local-reason"
    assert calls[0][1]["max_tokens"] == 4096
    assert calls[0][1]["temperature"] == 0.2
    assert calls[0][1]["extra_body"]["thinking_budget_tokens"] == 2048


def test_collection_runs_every_case_twice_without_holdout_or_generated_text() -> None:
    protocol, suite, report = _collect("current-control")

    validated = validate_report(report, protocol=protocol, suite=suite)

    assert len(validated["attempts"]) == 16
    assert validated["summary"]["overall"]["cases"] == 8
    assert validated["summary"]["overall"]["attempts"] == 16
    assert validated["holdout_loaded"] is False
    forbidden_fields = {
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
        "verdict",
        "summary",
        "issues",
        "unrelated_changes",
        "reasoning",
    }
    assert all(
        not (set(attempt) & forbidden_fields) for attempt in validated["attempts"]
    )


def test_report_validation_rejects_rehashed_profile_tamper() -> None:
    protocol, suite, report = _collect("current-control")
    tampered = deepcopy(report)
    tampered["role_profiles"]["planner"]["temperature"] = 0.0
    tampered["collection_sha256"] = stable_hash(
        {key: value for key, value in tampered.items() if key != "collection_sha256"}
    )

    with pytest.raises(ProfileTuningError, match="role_profiles"):
        validate_report(tampered, protocol=protocol, suite=suite)


def test_comparison_requires_every_frozen_profile() -> None:
    protocol, suite, reports = _all_reports()

    with pytest.raises(ProfileTuningError, match="requires every frozen profile"):
        compare_reports(reports[:-1], protocol=protocol, suite=suite)


def test_comparison_rejects_different_implementation_commit() -> None:
    protocol, suite, reports = _all_reports()
    tampered = deepcopy(reports[-1])
    tampered["implementation_commit"] = "b" * 40
    tampered["collection_sha256"] = stable_hash(
        {key: value for key, value in tampered.items() if key != "collection_sha256"}
    )
    reports[-1] = tampered

    with pytest.raises(ProfileTuningError, match="implementation_commit"):
        compare_reports(reports, protocol=protocol, suite=suite)


def test_accuracy_ranking_selects_improved_profiles_and_opens_holdout() -> None:
    protocol, suite, reports = _all_reports("accuracy")

    comparison = compare_reports(reports, protocol=protocol, suite=suite)

    assert comparison["selected_profiles"]["planner"] == "deterministic-accuracy"
    assert comparison["selected_profiles"]["reviewer"] in {
        "deterministic-accuracy",
        "role-depth-accuracy",
    }
    assert comparison["holdout_gate"]["open_roles"] == ["planner", "reviewer"]
    assert comparison["holdout_gate"]["combined_ready"] is True
    assert comparison["qualification_claim"] is None


def test_material_case_regression_blocks_role_holdout() -> None:
    protocol, suite, reports = _all_reports("regression")

    comparison = compare_reports(reports, protocol=protocol, suite=suite)

    assert comparison["selected_profiles"]["planner"] == "deterministic-accuracy"
    planner = comparison["role_decisions"]["planner"]
    assert planner["mean_score_gain"] > 0
    assert planner["material_regressions"]
    assert planner["holdout_ready"] is False
    assert "material_case_regression" in planner["reasons"]


def test_cli_exposes_development_collect_and_compare_only() -> None:
    options = parser().format_help()

    assert "collect" in options
    assert "compare" in options
    assert "holdout" not in options.casefold()
