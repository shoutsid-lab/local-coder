from pathlib import Path

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
    store.update_run(run_id, status="awaiting_human_review")

    details = store.run_details(run_id)

    assert details is not None
    assert details["status"] == "awaiting_human_review"
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
