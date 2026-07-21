import inspect
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.models import ModelRegistry
from runtime.skills import discover_skills
from runtime.state import StateStore
from runtime.tools import (
    ToolContext,
    Worktree,
    create_worktree,
    remove_worktree,
)

ROOT = Path(__file__).resolve().parent
CLI_SPEC = importlib.util.spec_from_file_location(
    "local_coder_cli", ROOT / "local-coder.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
local_coder = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(local_coder)
REVIEW_SPEC = importlib.util.spec_from_file_location(
    "review_diff_cli", ROOT / "review-diff.py"
)
assert REVIEW_SPEC is not None and REVIEW_SPEC.loader is not None
review_diff_cli = importlib.util.module_from_spec(REVIEW_SPEC)
REVIEW_SPEC.loader.exec_module(review_diff_cli)


def test_direct_cli_reenters_symlinked_project_virtualenv(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    base_python = tmp_path / "python"
    base_python.touch()
    venv_python = bin_dir / "python"
    venv_python.symlink_to(base_python)

    with (
        patch.object(local_coder, "VENV_PYTHON", venv_python),
        patch.object(local_coder.sys, "prefix", str(tmp_path / "base")),
        patch.object(local_coder.sys, "executable", str(base_python)),
        patch.dict(
            local_coder.os.environ,
            {"LOCAL_CODER_VENV_BOOTSTRAPPED": ""},
        ),
        patch.object(local_coder.os, "execve") as execve,
    ):
        local_coder.ensure_project_python()

    execve.assert_called_once()


def test_reviewer_accepts_fenced_valid_json() -> None:
    content = """Result:\n```json\n{
      "verdict": "pass",
      "summary": "The atomic change satisfies the task.",
      "issues": [],
      "unrelated_changes": []
    }\n```"""

    assert review_diff_cli.parse_review_content(content)["verdict"] == "pass"


def test_reviewer_rejects_invalid_json_shape() -> None:
    with pytest.raises(review_diff_cli.ReviewError):
        review_diff_cli.parse_review_content('{"result": "pass"}')


def test_reviewer_normalizes_verdict_only_response() -> None:
    review = review_diff_cli.parse_review_content('{"verdict": "pass"}')

    assert review["verdict"] == "pass"
    assert "without explanatory details" in review["summary"]
    assert review["issues"] == []
    assert review["unrelated_changes"] == []


def test_required_skills_load() -> None:
    skills = discover_skills(ROOT / ".local-coder" / "skills")

    assert set(skills) == {
        "explore-repository",
        "plan-change",
        "atomic-implementation",
        "test-and-repair",
        "review-change",
    }
    assert skills["atomic-implementation"].model == "local-fast"
    assert "delegate_aider" in skills["atomic-implementation"].tools
    assert "read-only adapter" in skills["explore-repository"].instructions
    assert "do not emit code" in skills["explore-repository"].instructions
    assert skills["explore-repository"].max_steps == 1
    assert skills["plan-change"].max_steps == 1


def test_managed_agents_accept_positional_additional_arguments(tmp_path: Path) -> None:
    from smolagents import CodeAgent

    from runtime.agents import ReadOnlyEvidenceAgent, build_agent_bundle

    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Inspect the repository",
        mode="agentic",
        repository=ROOT,
        base_branch="main",
    )
    context = ToolContext(
        root=ROOT,
        worktree=Worktree(ROOT, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=ROOT / "TASK.md",
    )
    bundle = build_agent_bundle(
        skills=discover_skills(ROOT / ".local-coder" / "skills"),
        context=context,
        models=ModelRegistry(),
        state=store,
        run_id=run_id,
    )

    parameters = inspect.signature(bundle.managed[0].__call__).parameters

    assert all(isinstance(agent, ReadOnlyEvidenceAgent) for agent in bundle.managed[:2])
    assert all(isinstance(agent, CodeAgent) for agent in bundle.managed[2:])
    assert all(
        agent.python_executor.timeout_seconds == 600 for agent in bundle.managed[2:]
    )
    assert "additional_args" in parameters
    assert bundle.manager.python_executor.timeout_seconds == 600
    assert bundle.manager.planning_interval is None

    explorer = bundle.managed[0]
    with patch.object(
        explorer.model,
        "generate",
        return_value=SimpleNamespace(content="evidence summary"),
    ) as generate:
        assert explorer("Inspect calculator.py", {"request": "context"}) == (
            "evidence summary"
        )
    messages = generate.call_args.args[0]
    assert "You have no tools" in messages[0]["content"]
    assert "def divide" in messages[1]["content"]
    assert generate.call_args.kwargs["tools_to_call_from"] is None
    details = store.run_details(run_id)
    assert details is not None
    assert details["tool_calls"][0]["agent_role"] == "explorer"
    assert details["tool_calls"][0]["tool_name"] == "read_file"

    implementer = bundle.managed[2]
    with patch.object(implementer, "run", return_value="done") as run:
        implementer("Replace one sentence in README.md")
    assert "editable_files='README.md'" in run.call_args.args[0]


def test_state_store_records_run_and_verification(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Add a feature",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    store.register_agent(
        run_id,
        role="explorer",
        skill="explore-repository",
        model_route="local-plan",
    )
    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="ok",
        duration_ms=1.5,
    )
    store.update_run(run_id, status="awaiting_approval")

    details = store.run_details(run_id)

    assert details is not None
    assert details["status"] == "awaiting_approval"
    assert details["agents"][0]["role"] == "explorer"
    assert details["verification"][0]["passed"] == 1


def test_tool_context_rejects_paths_outside_worktree(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Test path safety",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=worktree_path / "TASK.md",
    )

    try:
        context.read_file("../outside.txt")
    except ValueError as exc:
        assert "escapes the worktree" in str(exc)
    else:
        raise AssertionError("Unsafe path was accepted")


def test_delegate_aider_rejects_success_without_an_edit(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("unchanged\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    with (
        patch(
            "runtime.tools.command",
            return_value=SimpleNamespace(returncode=0, stdout="done", stderr=""),
        ),
        pytest.raises(RuntimeError, match="did not change an editable file"),
    ):
        context.delegate_aider("Add one sentence", "README.md")


def test_review_diff_uses_project_python(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Review a diff",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    run_dir = worktree_path / ".local-coder" / "runs" / run_id
    run_dir.mkdir(parents=True)
    task_file = run_dir / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    with patch(
        "runtime.tools.command",
        return_value=SimpleNamespace(returncode=0, stdout="reviewed", stderr=""),
    ) as command:
        assert context.review_diff() == "reviewed"

    assert command.call_args.args[0][0] == local_coder.sys.executable
    assert command.call_args.args[0][1] == "./review-diff.py"


def test_model_registry_exposes_role_aliases() -> None:
    registry = ModelRegistry()

    assert set(registry.routes) == {
        "local-fast",
        "local-plan",
        "local-review",
    }


def test_untracked_files_are_included_in_diff(tmp_path: Path) -> None:
    import subprocess

    from runtime.tools import collect_uncommitted_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    (tmp_path / "new_file.py").write_text("value = 1\n", encoding="utf-8")

    diff = collect_uncommitted_diff(tmp_path)

    assert "new_file.py" in diff
    assert "+value = 1" in diff


def test_codex_handoff_documents_exist() -> None:
    assert (ROOT / "AGENTS.md").is_file()
    assert (ROOT / "HANDOFF.md").is_file()


def test_create_worktree_shares_virtualenv(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=root,
        check=True,
    )
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (root / ".venv").mkdir()
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)

    worktree = create_worktree(root, run_id="abc123", task="Share environment")
    try:
        target = worktree.path / ".venv"
        assert target.is_symlink()
        assert target.resolve() == (root / ".venv").resolve()
    finally:
        remove_worktree(root, worktree, force=True)
