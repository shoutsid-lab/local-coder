import inspect
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.editor import AtomicEdit, EditorError, apply_edits, parse_edit_content
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
    assert "apply_atomic_edit" in skills["atomic-implementation"].tools
    assert "read-only adapter" in skills["explore-repository"].instructions
    assert "do not emit code" in skills["explore-repository"].instructions
    assert skills["explore-repository"].max_steps == 1
    assert skills["plan-change"].max_steps == 1


def test_native_editor_parses_strict_edit_json() -> None:
    edits = parse_edit_content(
        '{"edits":[{"path":"README.md","old_text":"before","new_text":"after"}]}'
    )

    assert edits == [AtomicEdit("README.md", "before", "after")]


def test_native_editor_accepts_one_exact_json_fence() -> None:
    edits = parse_edit_content("""```json
{"edits":[{"path":"README.md","old_text":"before","new_text":"after"}]}
```""")

    assert edits == [AtomicEdit("README.md", "before", "after")]


def test_native_editor_rejects_prose_around_json() -> None:
    with pytest.raises(EditorError, match="invalid JSON"):
        parse_edit_content(
            'Result: {"edits":[{"path":"README.md","old_text":"a","new_text":"b"}]}'
        )


def test_native_editor_applies_only_exact_unique_matches(tmp_path: Path) -> None:
    editable = tmp_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")

    changed = apply_edits(
        tmp_path,
        ["README.md"],
        [AtomicEdit("README.md", "before", "after")],
    )

    assert changed == ["README.md"]
    assert editable.read_text(encoding="utf-8") == "after\n"


def test_native_editor_fails_before_writing_ambiguous_edits(tmp_path: Path) -> None:
    editable = tmp_path / "README.md"
    editable.write_text("repeat repeat\n", encoding="utf-8")

    with pytest.raises(EditorError, match="match exactly once"):
        apply_edits(
            tmp_path,
            ["README.md"],
            [AtomicEdit("README.md", "repeat", "changed")],
        )

    assert editable.read_text(encoding="utf-8") == "repeat repeat\n"


def test_native_editor_rejects_protected_files(tmp_path: Path) -> None:
    protected = tmp_path / "example_contract.py"
    protected.write_text("before\n", encoding="utf-8")

    with pytest.raises(EditorError, match="Protected file"):
        apply_edits(
            tmp_path,
            ["example_contract.py"],
            [AtomicEdit("example_contract.py", "before", "after")],
        )


def test_native_editor_rejects_normalized_protected_aliases(tmp_path: Path) -> None:
    protected = tmp_path / "Makefile"
    protected.write_text("before\n", encoding="utf-8")

    with pytest.raises(EditorError, match="Protected file"):
        apply_edits(
            tmp_path,
            ["./Makefile"],
            [AtomicEdit("./Makefile", "before", "after")],
        )


def test_managed_agents_accept_positional_additional_arguments(tmp_path: Path) -> None:
    from smolagents import CodeAgent

    from runtime.agents import (
        ReadOnlyEvidenceAgent,
        ReadOnlyReviewAgent,
        build_agent_bundle,
    )

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
    assert all(isinstance(agent, CodeAgent) for agent in bundle.managed[2:4])
    assert isinstance(bundle.managed[4], ReadOnlyReviewAgent)
    assert all(
        agent.python_executor.timeout_seconds == 600 for agent in bundle.managed[2:4]
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
    assert "apply_atomic_edit exactly once" in run.call_args.args[0]
    assert "editable_files='README.md'" in run.call_args.args[0]


def test_reviewer_runs_only_fixed_read_only_gates(tmp_path: Path) -> None:
    from runtime.agents import ReadOnlyReviewAgent

    context = SimpleNamespace(
        inspect_diff=lambda: "diff",
        run_verification=lambda: "Verification: PASS",
        review_diff=lambda: '{"verdict": "pass"}',
    )
    reviewer = ReadOnlyReviewAgent(
        name="reviewer",
        description="Read-only review",
        context=context,
    )

    result = reviewer("Review README.md", {"ignored": True})

    assert result == 'diff\n\nVerification: PASS\n\n{"verdict": "pass"}'
    assert not hasattr(reviewer, "python_executor")


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


def _initialize_edit_test_repository(worktree_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=worktree_path,
        check=True,
    )
    subprocess.run(
        ["git", "add", "README.md", "TASK.md"], cwd=worktree_path, check=True
    )
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=worktree_path, check=True)


def test_atomic_editor_rejects_success_without_an_edit(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("unchanged\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
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
            "runtime.tools.request_and_apply",
            return_value=["README.md"],
        ),
        pytest.raises(RuntimeError, match="did not change an editable file"),
    ):
        context.apply_atomic_edit("Add one sentence", "README.md")


def test_atomic_editor_rejects_changes_outside_editable_scope(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
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

    def fake_editor(**_: object) -> list[str]:
        editable.write_text("after\n", encoding="utf-8")
        (worktree_path / "unexpected.py").write_text("", encoding="utf-8")
        return ["README.md"]

    with (
        patch("runtime.tools.request_and_apply", side_effect=fake_editor),
        pytest.raises(RuntimeError, match="outside the editable scope: unexpected.py"),
    ):
        context.apply_atomic_edit("Change one line", "README.md")


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


def test_staged_empty_files_are_included_in_diff(tmp_path: Path) -> None:
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

    (tmp_path / "staged_empty.py").touch()
    subprocess.run(["git", "add", "staged_empty.py"], cwd=tmp_path, check=True)

    diff = collect_uncommitted_diff(tmp_path)

    assert "staged_empty.py" in diff


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
