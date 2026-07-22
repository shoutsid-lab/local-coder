from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from runtime.state import StateStore
from runtime.tools import ToolContext, Worktree
from runtime.verification_evidence import summarize_verification_output

DSPY_WARNING = (
    "/tmp/.venv/lib/python3.14/site-packages/dspy/predict/avatar/signatures.py:12: "
    "DeprecationWarning: The 'prefix' argument in InputField/OutputField is "
    "deprecated and has no effect in DSPy. It will be removed in a future version."
)


def test_verification_evidence_collapses_known_dependency_warnings() -> None:
    raw = "\n".join(
        (
            "................................................................ [100%]",
            DSPY_WARNING,
            DSPY_WARNING.replace("signatures.py:12", "avatar_optimizer.py:30"),
            "152 passed, 11 warnings in 6.61s",
        )
    )

    evidence = summarize_verification_output(raw, passed=True)

    assert evidence.tests["passed"] == 152
    assert evidence.tests["warnings"] == 11
    assert evidence.warnings == {
        "known_third_party": 2,
        "unexpected": 0,
        "fingerprints": {"dspy-prefix-input-output-field": 2},
    }
    model_output = evidence.model_output()
    assert "2 known third-party DSPy deprecations" in model_output
    assert "avatar_optimizer.py" not in model_output
    assert "Raw verification output preserved" in model_output


def test_verification_evidence_keeps_failure_signal_and_unexpected_warning() -> None:
    raw = "\n".join(
        (
            "tests/test_example.py:8: UserWarning: check this behavior",
            "FAILED tests/test_example.py::test_value - AssertionError: mismatch",
            "make: *** [Makefile:21: test] Error 1",
            "1 failed, 2 passed, 1 warning in 0.20s",
        )
    )

    evidence = summarize_verification_output(raw, passed=False)

    assert evidence.tests["failed"] == 1
    assert evidence.tests["passed"] == 2
    assert evidence.warnings["unexpected"] == 1
    assert any("FAILED tests/test_example.py" in line for line in evidence.failures)
    assert evidence.model_output().startswith("Verification: FAIL")


def test_tool_context_preserves_raw_output_and_returns_compact_evidence(
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "TASK.md"
    task_file.write_text("Task", encoding="utf-8")
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Task",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(path=tmp_path, branch="agent/test", base_branch="main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )
    raw = f"{DSPY_WARNING}\n152 passed, 11 warnings in 1.00s"
    completed = subprocess.CompletedProcess(
        args=["make", "verify"],
        returncode=0,
        stdout=raw,
        stderr="",
    )

    with patch("runtime.tools.command", return_value=completed):
        result = context.run_verification()

    assert "152 passed" in result
    assert "signatures.py" not in result
    details = store.run_details(run_id)
    assert details is not None
    assert details["verification"][-1]["output"] == raw
    artifact = next(
        row for row in details["artifacts"] if row["kind"] == "verification_evidence"
    )
    payload = json.loads(artifact["content"])
    assert payload["warnings"]["known_third_party"] == 1
    assert payload["raw_sha256"] in result
