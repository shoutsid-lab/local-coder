from __future__ import annotations

from types import SimpleNamespace

import pytest

from runtime.dspy_lm import DSPY_ROUTES, build_dspy_lm


def test_dspy_lm_factory_uses_existing_litellm_alias() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def lm(model: str, **kwargs: object) -> SimpleNamespace:
        calls.append((model, kwargs))
        return SimpleNamespace(model=model)

    fake_dspy = SimpleNamespace(LM=lm)
    result = build_dspy_lm("local-review", dspy_module=fake_dspy)

    assert result.model == "openai/local-review"
    assert calls == [
        (
            "openai/local-review",
            {
                "model_type": "chat",
                "api_base": "http://127.0.0.1:4000/v1",
                "api_key": "local",
                "temperature": 0,
                "max_tokens": 2048,
                "cache": False,
                "num_retries": 0,
            },
        )
    ]
    assert DSPY_ROUTES == {"local-fast", "local-plan", "local-review"}


def test_dspy_lm_factory_rejects_untrusted_route() -> None:
    with pytest.raises(ValueError, match="Unsupported DSPy route"):
        build_dspy_lm("cloud-review", dspy_module=SimpleNamespace(LM=lambda: None))


def test_reviewer_signature_and_program_contract() -> None:
    dspy = pytest.importorskip("dspy")
    from runtime.dspy_programs.reviewer import ReviewerProgram, ReviewerSignature

    assert list(ReviewerSignature.input_fields) == [
        "task",
        "changed_files",
        "verification_passed",
        "verification_output",
        "diff",
    ]
    assert list(ReviewerSignature.output_fields) == [
        "verdict",
        "summary",
        "issues",
        "unrelated_changes",
    ]
    program = ReviewerProgram()
    assert isinstance(program.predict, dspy.Predict)
    assert not isinstance(program.predict, dspy.ChainOfThought)


def test_live_e2e_requires_dspy_reviewer_backend_marker() -> None:
    from runtime.live_e2e import dspy_reviewer_backends

    metrics = [
        {
            "route": "local-review",
            "metadata": '{"source":"dspy-reviewer","program":"ReviewerProgram",'
            '"adapter":"JSONAdapter"}',
        },
        {
            "route": "local-review",
            "metadata": '{"source":"dspy-reviewer","program":"ReviewerProgram",'
            '"adapter":"JSONAdapter"}',
        },
        {"route": "local-review", "metadata": "not-json"},
        {
            "route": "local-plan",
            "metadata": '{"source":"dspy-reviewer","program":"ReviewerProgram",'
            '"adapter":"JSONAdapter"}',
        },
    ]

    assert dspy_reviewer_backends(metrics) == ["ReviewerProgram/JSONAdapter"]


def test_live_e2e_report_rejects_stale_summary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import live_e2e_report

    summary = tmp_path / "latest-summary.json"
    summary.write_text(
        '{"passed": true, "base_commit": "old-commit"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(live_e2e_report, "current_commit", lambda: "new-commit")

    with pytest.raises(RuntimeError, match="Stale live E2E summary"):
        live_e2e_report.load_latest_summary(summary)


def test_live_e2e_report_accepts_current_summary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import live_e2e_report

    summary = tmp_path / "latest-summary.json"
    summary.write_text(
        '{"passed": false, "base_commit": "current-commit"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(live_e2e_report, "current_commit", lambda: "current-commit")

    assert live_e2e_report.load_latest_summary(summary) == {
        "passed": False,
        "base_commit": "current-commit",
    }


def test_explorer_signature_and_program_contract() -> None:
    dspy = pytest.importorskip("dspy")
    from runtime.dspy_programs.explorer import (
        ExplorerProgram,
        ExplorerSignature,
    )

    assert list(ExplorerSignature.input_fields) == [
        "task",
        "delegated_task",
        "repository_evidence",
    ]
    assert list(ExplorerSignature.output_fields) == [
        "findings",
        "relevant_files",
        "constraints",
        "unresolved_questions",
    ]
    program = ExplorerProgram()
    assert isinstance(program.predict, dspy.ChainOfThought)


def test_planner_signature_and_program_contract() -> None:
    dspy = pytest.importorskip("dspy")
    from runtime.dspy_programs.planner import PlannerProgram, PlannerSignature

    assert list(PlannerSignature.input_fields) == [
        "task",
        "delegated_task",
        "repository_evidence",
    ]
    assert list(PlannerSignature.output_fields) == [
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
    ]
    program = PlannerProgram()
    assert isinstance(program.predict, dspy.ChainOfThought)


def test_live_e2e_requires_all_read_only_dspy_backend_markers() -> None:
    from runtime.live_e2e import (
        dspy_explorer_backends,
        dspy_planner_backends,
        dspy_reviewer_backends,
    )

    metrics = [
        {
            "route": "local-plan",
            "metadata": '{"source":"dspy-explorer","program":'
            '"ExplorerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-plan",
            "metadata": '{"source":"dspy-planner","program":'
            '"PlannerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-review",
            "metadata": '{"source":"dspy-reviewer","program":'
            '"ReviewerProgram","adapter":"JSONAdapter"}',
        },
    ]

    assert dspy_explorer_backends(metrics) == ["ExplorerProgram/JSONAdapter"]
    assert dspy_planner_backends(metrics) == ["PlannerProgram/JSONAdapter"]
    assert dspy_reviewer_backends(metrics) == ["ReviewerProgram/JSONAdapter"]


def test_implementer_signature_and_program_contract() -> None:
    dspy = pytest.importorskip("dspy")
    from runtime.dspy_programs.implementer import (
        ImplementerProgram,
        ImplementerSignature,
    )

    assert list(ImplementerSignature.input_fields) == [
        "task",
        "instruction",
        "editable_files",
        "file_contents",
    ]
    assert list(ImplementerSignature.output_fields) == ["edits"]
    program = ImplementerProgram()
    assert isinstance(program.predict, dspy.Predict)
    assert not isinstance(program.predict, dspy.ChainOfThought)


def test_live_e2e_requires_dspy_implementer_backend_marker() -> None:
    from runtime.live_e2e import dspy_implementer_backends

    metrics = [
        {
            "route": "local-fast",
            "metadata": '{"source":"dspy-implementer","program":'
            '"ImplementerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-fast",
            "metadata": '{"source":"dspy-implementer","program":'
            '"ImplementerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-plan",
            "metadata": '{"source":"dspy-implementer","program":'
            '"ImplementerProgram","adapter":"JSONAdapter"}',
        },
    ]

    assert dspy_implementer_backends(metrics) == ["ImplementerProgram/JSONAdapter"]
