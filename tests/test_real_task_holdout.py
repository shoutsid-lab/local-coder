from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

import evaluation.real_task_holdout as holdout_module
from evaluation.outcomes import stable_hash
from evaluation.real_task_corpus import CaseSuite
from evaluation.real_task_development import (
    load_development_suite,
    load_protocol as load_development_protocol,
)
from evaluation.real_task_holdout import (
    DEFAULT_PROTOCOL_PATH,
    HoldoutQualificationError,
    collect_report,
    compare_reports,
    load_protocol,
    parser,
    reserve_holdout_run,
    validate_report,
    validate_selection_report,
)
from evaluation.real_task_prompt_tuning import load_protocol as load_prompt_protocol


def _test_suite(protocol) -> CaseSuite:
    development_protocol = load_development_protocol()
    development = load_development_suite(development_protocol)
    planners = [case for case in development.cases if case.role == "planner"][:2]
    reviewers = [case for case in development.cases if case.role == "reviewer"][:2]
    return CaseSuite(
        corpus_id=development.corpus_id,
        suite_id=protocol.suite_id,
        visibility="holdout",
        cases=tuple(planners + reviewers),
        suite_hash=protocol.suite_sha256,
        path=Path("trusted-holdout.json"),
    )


def _perfect_prediction(case) -> dict[str, object]:
    oracle = case.oracle
    if case.role == "planner":
        return {
            "instruction": " ".join(oracle["required_instruction_terms"]),
            "editable_files": list(oracle["editable_files"]),
            "acceptance_criteria": list(oracle["required_acceptance_terms"]),
            "depends_on": list(oracle["depends_on"]),
        }
    return {
        "verdict": oracle["verdict"],
        "summary": "Evidence-backed review.",
        "issues": [
            f"{path}: definite issue" for path in oracle["required_issue_paths"]
        ],
        "unrelated_changes": [
            f"{path}: unrelated change" for path in oracle["required_unrelated_paths"]
        ],
    }


def _runner_factory(suite, *, imperfect_control: bool):
    by_task = {case.inputs["task"]: case for case in suite.cases}
    first_planner = next(case.case_id for case in suite.cases if case.role == "planner")
    first_reviewer = next(
        case.case_id for case in suite.cases if case.role == "reviewer"
    )

    def factory(role: str, instructions: str | None):
        del role

        def run(*, lm: object, **inputs: object) -> dict[str, object]:
            del lm
            case = by_task[inputs["task"]]
            prediction = _perfect_prediction(case)
            if imperfect_control and instructions is None:
                if case.case_id == first_planner:
                    prediction["acceptance_criteria"] = ["generic verification"]
                if case.case_id == first_reviewer:
                    prediction["verdict"] = "pass"
            return prediction

        return run

    return factory


def _service(protocol, subject_name: str) -> dict[str, object]:
    subject = protocol.subjects[subject_name]
    return {
        "build_info": "test-build",
        "configured_context_tokens": 32768,
        "llama_alias": subject.llama_alias,
        "model_file": subject.model_file,
        "total_slots": 1,
    }


def _report(protocol, prompt_protocol, suite, subject_name: str):
    selection = {"comparison_sha256": protocol.selection.comparison_sha256}
    return collect_report(
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        selection_report=selection,
        suite=suite,
        subject_name=subject_name,
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity=_service(protocol, subject_name),
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lambda _route, _profile: object(),
        runner_factory=_runner_factory(suite, imperfect_control=True),
    )


def _selection_report(protocol) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": 1,
        "comparison_kind": protocol.selection.comparison_kind,
        "protocol_id": protocol.selection.prompt_protocol_id,
        "protocol_sha256": protocol.selection.prompt_protocol_sha256,
        "suite_id": "real-task-development-v1",
        "suite_sha256": "1" * 64,
        "environment_id": "test-machine",
        "implementation_commit": protocol.selection.implementation_commit,
        "service_identity": {},
        "generation_profiles": {},
        "report_hashes": dict(protocol.selection.report_hashes),
        "prompt_profile_summaries": {},
        "selected_prompt_profiles": {
            **dict(protocol.selection.selected_prompt_profiles),
            "overall": "evidence-completeness",
        },
        "role_decisions": {
            role: {
                "selected_prompt_profile": (
                    protocol.selection.selected_prompt_profiles[role]
                ),
                "holdout_ready": True,
            }
            for role in ("planner", "reviewer")
        },
        "mixed_role_projection": {},
        "holdout_gate": {
            "open_roles": ["planner", "reviewer"],
            "combined_ready": False,
            "combined_reasons": ["insufficient_stable_case_success_rate"],
        },
        "qualification_claim": None,
    }
    report["comparison_sha256"] = stable_hash(report)
    return report


def test_protocol_freezes_one_shot_subjects_and_selection() -> None:
    protocol = load_protocol(DEFAULT_PROTOCOL_PATH)

    assert protocol.protocol_id == "track-g-holdout-qualification-v1"
    assert protocol.attempts_per_case == 1
    assert protocol.subjects["baseline"].routes == {
        "planner": "local-plan",
        "reviewer": "local-review",
    }
    assert protocol.subjects["candidate"].prompt_profiles == {
        "planner": "evidence-completeness",
        "reviewer": "field-checklist",
    }
    assert protocol.subjects["candidate"].route_profiles == (
        load_prompt_protocol().generation_profiles
    )
    assert protocol.policy.minimum_role_mean_gain == 0.05


def test_selection_report_is_hash_bound_and_opens_both_roles() -> None:
    protocol = load_protocol()
    prompt_protocol = load_prompt_protocol()
    report = _selection_report(protocol)
    selection = replace(
        protocol.selection,
        comparison_sha256=report["comparison_sha256"],
    )
    test_protocol = replace(protocol, selection=selection)

    validated = validate_selection_report(
        report,
        protocol=test_protocol,
        prompt_protocol=prompt_protocol,
    )

    assert validated["holdout_gate"]["open_roles"] == ["planner", "reviewer"]
    tampered = copy.deepcopy(report)
    tampered["holdout_gate"]["open_roles"] = ["planner"]
    with pytest.raises(HoldoutQualificationError, match="hash does not match"):
        validate_selection_report(
            tampered,
            protocol=test_protocol,
            prompt_protocol=prompt_protocol,
        )


def test_collection_runs_all_four_cases_once_without_retaining_content() -> None:
    protocol = load_protocol()
    prompt_protocol = load_prompt_protocol()
    suite = _test_suite(protocol)

    report = _report(protocol, prompt_protocol, suite, "candidate")
    validated = validate_report(
        report,
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
    )

    assert len(validated["attempts"]) == 4
    assert validated["holdout_loaded"] is True
    assert validated["one_shot"] is True
    for case in suite.cases:
        assert case.task not in str(validated)


def test_report_validation_rejects_rehashed_summary_tampering() -> None:
    protocol = load_protocol()
    prompt_protocol = load_prompt_protocol()
    suite = _test_suite(protocol)
    report = _report(protocol, prompt_protocol, suite, "baseline")
    tampered = copy.deepcopy(report)
    tampered["summary"]["overall"]["mean_score"] = 0.0
    tampered.pop("collection_sha256")
    tampered["collection_sha256"] = stable_hash(tampered)

    with pytest.raises(HoldoutQualificationError, match="summary does not match"):
        validate_report(
            tampered,
            protocol=protocol,
            prompt_protocol=prompt_protocol,
            suite=suite,
        )


def test_comparison_qualifies_both_roles_on_accuracy_gain() -> None:
    protocol = load_protocol()
    prompt_protocol = load_prompt_protocol()
    suite = _test_suite(protocol)
    baseline = _report(protocol, prompt_protocol, suite, "baseline")
    candidate = _report(protocol, prompt_protocol, suite, "candidate")

    comparison = compare_reports(
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
        baseline_value=baseline,
        candidate_value=candidate,
    )

    assert comparison["qualified_roles"] == ["planner", "reviewer"]
    assert comparison["combined_qualified"] is True
    assert comparison["qualification_claim"] == "planner_and_reviewer"
    assert comparison["route_activation"] is None


def test_comparison_blocks_a_material_candidate_regression() -> None:
    protocol = load_protocol()
    prompt_protocol = load_prompt_protocol()
    suite = _test_suite(protocol)
    baseline = _report(protocol, prompt_protocol, suite, "baseline")
    candidate = _report(protocol, prompt_protocol, suite, "candidate")
    reviewer = next(
        item for item in candidate["attempts"] if item["role"] == "reviewer"
    )
    reviewer["dimensions"] = {
        "schema_valid": True,
        "verdict_match": False,
        "required_issue_paths_found": False,
        "required_unrelated_paths_found": False,
        "forbidden_issue_paths_absent": True,
    }
    reviewer["case_success"] = False
    reviewer["score"] = 0.4
    reviewer["failure_codes"] = [
        "verdict_mismatch",
        "required_issue_path_missing",
        "required_unrelated_path_missing",
    ]
    candidate["summary"] = holdout_module.summarize(candidate["attempts"])
    candidate.pop("collection_sha256")
    candidate["collection_sha256"] = stable_hash(candidate)

    comparison = compare_reports(
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
        baseline_value=baseline,
        candidate_value=candidate,
    )

    assert comparison["role_decisions"]["reviewer"]["qualified"] is False
    assert "material_case_regression" in (
        comparison["role_decisions"]["reviewer"]["reasons"]
    )


def test_reservation_is_exclusive_and_contains_no_holdout_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = load_protocol()
    monkeypatch.setattr(holdout_module, "DEFAULT_RECEIPT_DIR", tmp_path)

    receipt = reserve_holdout_run(
        protocol=protocol,
        subject="baseline",
        environment_id="test-machine",
        implementation_commit="a" * 40,
    )

    assert receipt["status"] == "reserved"
    assert "oracle" not in str(receipt)
    with pytest.raises(Exception, match="Refusing to overwrite"):
        reserve_holdout_run(
            protocol=protocol,
            subject="baseline",
            environment_id="test-machine",
            implementation_commit="a" * 40,
        )


def test_cli_requires_explicit_trusted_holdout_and_has_no_role_subset() -> None:
    collect = parser().parse_args(
        [
            "collect",
            "--subject",
            "candidate",
            "--environment-id",
            "test-machine",
            "--holdout-suite",
            "trusted.json",
        ]
    )

    assert collect.holdout_suite == Path("trusted.json")
    option_strings = {
        option
        for action in parser()._subparsers._group_actions[0].choices["collect"]._actions
        for option in action.option_strings
    }
    assert "--role" not in option_strings
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "collect",
                "--subject",
                "candidate",
                "--environment-id",
                "test-machine",
            ]
        )
