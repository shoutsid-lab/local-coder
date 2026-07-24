from __future__ import annotations

import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evaluation.real_task_prompt_tuning import (
    DEFAULT_PROTOCOL_PATH,
    PromptTuningError,
    _program_runner,
    _set_program_instructions,
    collect_report,
    compare_reports,
    load_development_suite,
    load_protocol,
    parser,
    validate_report,
)
from evaluation.outcomes import stable_hash


class FakeSignature:
    def __init__(self, instructions: str = "baseline") -> None:
        self.instructions = instructions

    def with_instructions(self, instructions: str) -> "FakeSignature":
        return FakeSignature(instructions)


class FakePredictor:
    def __init__(self) -> None:
        self.signature = FakeSignature()


class FakeProgram:
    last: "FakeProgram | None" = None

    def __init__(self) -> None:
        self.predict = FakePredictor()
        self.calls: list[dict[str, object]] = []
        FakeProgram.last = self

    def named_predictors(self) -> list[tuple[str, object]]:
        return [("predict", self.predict)]

    def __call__(self, **inputs: object) -> dict[str, object]:
        self.calls.append(inputs)
        return {"instructions": self.predict.signature.instructions}


FAKE_DSPY = SimpleNamespace(
    JSONAdapter=lambda: object(),
    context=lambda **_kwargs: nullcontext(),
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
            f"{path}: outside requested scope"
            for path in oracle["required_unrelated_paths"]
        ],
    }


def _runner_factory(suite, *, mode: str):
    by_task = {case.inputs["task"]: case for case in suite.cases}

    def factory(role: str, instructions: str | None):
        del role

        def run(*, lm: object, **inputs: object) -> dict[str, object]:
            del lm
            case = by_task[inputs["task"]]
            prediction = _perfect_prediction(case)
            is_control = instructions is None
            is_field = instructions is not None and "every output field" in instructions
            if mode == "varied" and is_control:
                if case.case_id in {
                    "dev-planner-unused-subject-assignment",
                    "dev-reviewer-readme-qualification-pollution",
                }:
                    if case.role == "planner":
                        prediction["acceptance_criteria"] = ["generic verification"]
                    else:
                        prediction["unrelated_changes"] = []
            if mode == "varied" and is_field:
                if case.case_id == "dev-planner-reasoning-truncation-contract":
                    prediction["acceptance_criteria"] = ["generic verification"]
            return prediction

        return run

    return factory


def _report(protocol, suite, profile_id: str, *, mode: str = "perfect"):
    return collect_report(
        protocol=protocol,
        suite=suite,
        prompt_profile_id=profile_id,
        environment_id="test-machine",
        implementation_commit="a" * 40,
        service_identity={
            "build_info": "test-build",
            "configured_context_tokens": 32768,
            "llama_alias": "local-reason",
            "model_file": protocol.candidate_model_file,
            "total_slots": 1,
        },
        prompt_state={"planner": None, "reviewer": None},
        lm_factory=lambda _route, _profile: object(),
        runner_factory=_runner_factory(suite, mode=mode),
    )


def test_protocol_freezes_prompt_only_experiment() -> None:
    protocol = load_protocol(DEFAULT_PROTOCOL_PATH)

    assert protocol.protocol_id == "track-g-qwythos-prompt-tuning-v1"
    assert protocol.prompt_profiles["code-control"].planner is None
    assert protocol.prompt_profiles["code-control"].reviewer is None
    assert protocol.generation_profiles["planner"].reasoning_tokens == 1024
    assert protocol.generation_profiles["reviewer"].temperature == 0.0
    assert set(protocol.prompt_profiles) == {
        "code-control",
        "evidence-completeness",
        "field-checklist",
    }


def test_instruction_override_replaces_only_predictor_signature() -> None:
    program = FakeProgram()

    _set_program_instructions(program, "candidate instruction")

    assert program.predict.signature.instructions == "candidate instruction"


def test_program_runner_uses_json_adapter_and_inert_instruction() -> None:
    runner = _program_runner(
        "planner",
        "candidate instruction",
        dspy_module=FAKE_DSPY,
        program_factory=FakeProgram,
    )

    result = runner(lm=object(), task="task")

    assert result == {"instructions": "candidate instruction"}
    assert FakeProgram.last is not None
    assert FakeProgram.last.calls == [{"task": "task"}]


def test_collection_scores_all_cases_without_holdout() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)

    report = _report(protocol, suite, "evidence-completeness")

    assert report["holdout_loaded"] is False
    assert len(report["attempts"]) == 16
    assert report["summary"]["overall"]["mean_score"] == 1.0
    assert report["summary"]["overall"]["stable_case_success_rate"] == 1.0
    validate_report(report, protocol=protocol, suite=suite)


def test_collection_rejects_active_prompt_state() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)

    with pytest.raises(PromptTuningError, match="requires no active deployed"):
        collect_report(
            protocol=protocol,
            suite=suite,
            prompt_profile_id="code-control",
            environment_id="test-machine",
            implementation_commit="a" * 40,
            service_identity={},
            prompt_state={
                "planner": {
                    field: "value"
                    for field in (
                        "activation_id",
                        "campaign_id",
                        "build_id",
                        "evaluation_id",
                        "candidate_instruction_hash",
                        "program_hash",
                    )
                },
                "reviewer": None,
            },
            lm_factory=lambda _route, _profile: object(),
            runner_factory=_runner_factory(suite, mode="perfect"),
        )


def test_validation_rejects_rehashed_summary_tampering() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)
    report = _report(protocol, suite, "evidence-completeness")
    tampered = copy.deepcopy(report)
    tampered["summary"]["overall"]["mean_score"] = 0.5
    tampered.pop("collection_sha256")
    tampered["collection_sha256"] = stable_hash(tampered)

    with pytest.raises(PromptTuningError, match="summary does not match"):
        validate_report(tampered, protocol=protocol, suite=suite)


def test_comparison_selects_improved_prompt_and_opens_gate() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)
    reports = [
        _report(protocol, suite, profile_id, mode="varied")
        for profile_id in protocol.prompt_profiles
    ]

    comparison = compare_reports(reports, protocol=protocol, suite=suite)

    assert comparison["selected_prompt_profiles"]["planner"] == (
        "evidence-completeness"
    )
    assert comparison["selected_prompt_profiles"]["reviewer"] in {
        "evidence-completeness",
        "field-checklist",
    }
    assert comparison["holdout_gate"]["combined_ready"] is True
    assert comparison["holdout_gate"]["open_roles"] == ["planner", "reviewer"]
    assert comparison["qualification_claim"] is None


def test_comparison_requires_every_frozen_prompt_profile() -> None:
    protocol = load_protocol()
    suite = load_development_suite(protocol)
    reports = [_report(protocol, suite, "code-control")]

    with pytest.raises(PromptTuningError, match="requires every frozen"):
        compare_reports(reports, protocol=protocol, suite=suite)


def test_cli_exposes_no_holdout_argument() -> None:
    option_strings = {
        option for action in parser()._actions for option in action.option_strings
    }
    assert "--holdout" not in option_strings
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "collect",
                "--prompt-profile",
                "code-control",
                "--environment-id",
                "test-machine",
                "--holdout",
                "secret.json",
            ]
        )
