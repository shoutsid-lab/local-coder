from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.agents import (
    DSPyRepairerAgent,
    _latest_failed_verification,
    _repairer_files,
)
from runtime.live_e2e import dspy_repairer_backends
from runtime.state import StateStore
from runtime.tools import ToolContext, Worktree


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


def _repair_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "canary.txt").write_text(
        "REPAIR_SENTINEL=after\n",
        encoding="utf-8",
    )
    task_file = repository / "TASK.md"
    task_file.write_text(
        "# Agent Task\n\nKeep canary.txt at REPAIR_SENTINEL=after.\n",
        encoding="utf-8",
    )
    (repository / "Makefile").write_text(
        "verify:\n\t@grep -qx 'REPAIR_SENTINEL=after' canary.txt\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    return repository, task_file


def test_repairer_signature_and_program_contract() -> None:
    dspy = pytest.importorskip("dspy")
    from runtime.dspy_programs.repairer import RepairerProgram, RepairerSignature

    assert list(RepairerSignature.input_fields) == [
        "task",
        "delegated_task",
        "verification_output",
        "diff",
        "editable_files",
        "file_contents",
    ]
    assert list(RepairerSignature.output_fields) == ["diagnosis", "edits"]
    program = RepairerProgram()
    assert isinstance(program.predict, dspy.Predict)
    assert not isinstance(program.predict, dspy.ChainOfThought)


def test_live_e2e_requires_dspy_repairer_backend_marker() -> None:
    metrics = [
        {
            "route": "local-fast",
            "metadata": '{"source":"dspy-repairer","program":'
            '"RepairerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-fast",
            "metadata": '{"source":"dspy-repairer","program":'
            '"RepairerProgram","adapter":"JSONAdapter"}',
        },
        {
            "route": "local-plan",
            "metadata": '{"source":"dspy-repairer","program":'
            '"RepairerProgram","adapter":"JSONAdapter"}',
        },
    ]

    assert dspy_repairer_backends(metrics) == ["RepairerProgram/JSONAdapter"]


def test_repairer_requires_latest_failed_verification(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Repair",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )

    with pytest.raises(RuntimeError, match="recorded verification failure"):
        _latest_failed_verification(store, run_id)

    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="Verification passed",
        duration_ms=1,
    )
    with pytest.raises(RuntimeError, match="cannot run after verification passed"):
        _latest_failed_verification(store, run_id)


def test_repairer_files_rejects_broad_unfocused_diff(tmp_path: Path) -> None:
    repository, task_file = _repair_repository(tmp_path)
    for name in ("one.py", "two.py", "three.py"):
        (repository / name).write_text("before\n", encoding="utf-8")
    context = ToolContext(
        root=repository,
        worktree=Worktree(repository, "agent/test", "main"),
        run_id="run",
        state=StateStore(tmp_path / "agent.db"),
        task_file=task_file,
        agent_role="repairer",
    )
    diff = "\n".join(
        f"diff --git a/{name} b/{name}" for name in ("one.py", "two.py", "three.py")
    )

    with pytest.raises(RuntimeError, match="focused request"):
        _repairer_files("Repair the failure", "failure", diff, context)


def test_dspy_repairer_applies_one_batch_and_reverifies(tmp_path: Path) -> None:
    repository, task_file = _repair_repository(tmp_path)
    canary = repository / "canary.txt"
    canary.write_text("REPAIR_SENTINEL=before\n", encoding="utf-8")
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Repair canary.txt",
        mode="agentic",
        repository=repository,
        base_branch="main",
    )
    store.register_agent(
        run_id,
        role="repairer",
        skill="test-and-repair",
        model_route="local-fast",
    )
    context = ToolContext(
        root=repository,
        worktree=Worktree(repository, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        agent_role="repairer",
        allowed_edit_paths=frozenset({"canary.txt"}),
    )
    assert context.run_verification().startswith("Verification: FAIL")

    def usage() -> dict[str, dict[str, int]]:
        return {
            "openai/local-fast": {
                "prompt_tokens": 17,
                "completion_tokens": 11,
            }
        }

    prediction = SimpleNamespace(
        diagnosis="canary.txt contains the failing before sentinel.",
        edits=[
            {
                "path": "canary.txt",
                "old_text": "REPAIR_SENTINEL=before",
                "new_text": "REPAIR_SENTINEL=after",
            }
        ],
        get_lm_usage=usage,
    )
    agent = DSPyRepairerAgent(
        name="repairer",
        description="Repair one failure",
        activate_skill=lambda: SimpleNamespace(),
        context=context,
        model_route="local-fast",
        lm_factory=lambda: SimpleNamespace(model="openai/local-fast"),
        program_runner=lambda **_kwargs: prediction,
        program_name="RepairerProgram",
        state=store,
        run_id=run_id,
    )

    result = agent(
        "Repair canary.txt by replacing REPAIR_SENTINEL=before with "
        "REPAIR_SENTINEL=after."
    )

    assert result.startswith("Repair succeeded:")
    assert "Diagnosis: canary.txt contains the failing before sentinel." in result
    assert canary.read_text(encoding="utf-8") == "REPAIR_SENTINEL=after\n"
    details = store.run_details(run_id)
    assert details is not None
    assert [item["passed"] for item in details["verification"]] == [0, 1]
    editor_calls = [
        call
        for call in details["tool_calls"]
        if call["tool_name"] == "apply_atomic_edit"
    ]
    assert len(editor_calls) == 1
    assert editor_calls[0]["status"] == "success"
    assert json.loads(editor_calls[0]["arguments"])["source"] == "dspy-repairer"
    metric = details["model_metrics"][-1]
    assert metric["route"] == "local-fast"
    assert metric["prompt_tokens"] == 17
    assert metric["completion_tokens"] == 11
    assert json.loads(metric["metadata"]) == {
        "source": "dspy-repairer",
        "program": "RepairerProgram",
        "adapter": "JSONAdapter",
        "status": "success",
    }
