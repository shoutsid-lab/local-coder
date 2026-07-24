from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.search.contracts import RepositorySearchHit, RepositorySearchRequest
from runtime.search.engine import RepositorySearchEngine
from runtime.search.git_backend import GitFallbackBackend
from runtime.search.profile import load_search_profile
from runtime.search.query_router import build_query_plan
from runtime.search.ripgrep_backend import RipgrepBackend
from runtime.state import StateStore
from runtime.tools import ToolContext, Worktree

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "user.email", "tests@example.com")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "Makefile").write_text("verify:\n\tpytest -q\n", encoding="utf-8")
    (root / "alpha.py").write_text(
        "def calculate_total(values):\n    return sum(values)\n",
        encoding="utf-8",
    )
    (root / "ignored.txt").write_text("calculate_total\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00calculate_total\x00")
    (root / "bad.txt").write_bytes(b"calculate_total\xff\n")
    _git(
        root,
        "add",
        ".gitignore",
        "Makefile",
        "alpha.py",
        "binary.bin",
        "bad.txt",
    )
    _git(root, "commit", "-qm", "fixture")
    return root


def test_query_router_derives_path_symbol_and_exact_text() -> None:
    plan = build_query_plan(
        "Fix runtime/search/engine.py where RepositorySearchEngine emits "
        '"stale result".'
    )
    assert any(item.mode == "filename" and item.query == "engine.py" for item in plan)
    assert any(
        item.mode == "symbol" and item.query == "RepositorySearchEngine"
        for item in plan
    )
    assert any(item.mode == "text" and item.query == "stale result" for item in plan)


def test_ripgrep_search_is_structured_bounded_and_ignores_binary(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    backend = RipgrepBackend(load_search_profile(ROOT))
    if not backend.available:
        pytest.skip("ripgrep is not installed")
    request = RepositorySearchRequest(
        query="calculate_total",
        repository_ids=("active",),
        worktree=root,
        mode="text",
        path_globs=("*.py",),
        limit=1,
    )
    hits = backend.search(request, repository_id="active")
    assert len(hits) == 1
    assert hits[0].path == "alpha.py"
    assert hits[0].start_line == 1
    assert hits[0].backend == "ripgrep"


def test_ripgrep_supports_regex_and_filename_modes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    backend = RipgrepBackend(load_search_profile(ROOT))
    if not backend.available:
        pytest.skip("ripgrep is not installed")
    regex_hits = backend.search(
        RepositorySearchRequest(
            query=r"calculate_[a-z]+",
            repository_ids=("active",),
            worktree=root,
            mode="regex",
        ),
        repository_id="active",
    )
    filename_hits = backend.search(
        RepositorySearchRequest(
            query="alpha.py",
            repository_ids=("active",),
            worktree=root,
            mode="filename",
        ),
        repository_id="active",
    )
    assert "alpha.py" in {hit.path for hit in regex_hits}
    assert filename_hits[0].match_kind == "filename"
    assert filename_hits[0].score == 110.0


def test_filename_search_preserves_hidden_and_extensionless_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    backend = RipgrepBackend(load_search_profile(ROOT))
    if not backend.available:
        pytest.skip("ripgrep is not installed")

    hidden = backend.search(
        RepositorySearchRequest(
            query=".gitignore",
            repository_ids=("active",),
            worktree=root,
            mode="filename",
        ),
        repository_id="active",
    )
    repository_map = backend.search(
        RepositorySearchRequest(
            query=".",
            repository_ids=("active",),
            worktree=root,
            mode="filename",
        ),
        repository_id="active",
    )

    assert hidden[0].path == ".gitignore"
    assert {hit.path for hit in repository_map} >= {".gitignore", "Makefile"}


def test_git_fallback_includes_untracked_and_skips_malformed_utf8(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "new.py").write_text("calculate_total = 1\n", encoding="utf-8")
    backend = GitFallbackBackend()
    hits = backend.search(
        RepositorySearchRequest(
            query="calculate_total",
            repository_ids=("active",),
            worktree=root,
            mode="text",
            limit=20,
        ),
        repository_id="active",
    )
    assert {hit.path for hit in hits} >= {"alpha.py", "new.py"}
    assert "bad.txt" not in {hit.path for hit in hits}


def test_tool_context_search_uses_current_untracked_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "current.py").write_text(
        "current_untracked_search_value = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "LOCAL_CODER_SEARCH_HOME",
        str(tmp_path / "search-state"),
    )
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Search current bytes",
        mode="agentic",
        repository=repository,
        base_branch="main",
    )
    task_file = repository / "TASK.md"
    task_file.write_text("Search current bytes.\n", encoding="utf-8")
    context = ToolContext(
        root=ROOT,
        worktree=Worktree(repository, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    output = context.search_repository(
        "current_untracked_search_value",
        "*.py",
    )

    assert "current.py:1:current_untracked_search_value = 1" in output


def test_git_snapshot_tracks_renames_with_spaces(tmp_path: Path) -> None:
    from runtime.search.git_state import snapshot

    root = _repository(tmp_path)
    old_path = root / "old name.py"
    old_path.write_text("renamed_value = 1\n", encoding="utf-8")
    _git(root, "add", "old name.py")
    _git(root, "commit", "-qm", "add spaced path")
    _git(root, "mv", "old name.py", "new name.py")

    current = snapshot(root)

    assert "new name.py" in current.renamed
    assert "new name.py" in current.modified
    assert "old name.py" in current.deleted


def test_git_snapshot_hash_changes_with_dirty_file_content(tmp_path: Path) -> None:
    from runtime.search.git_state import snapshot

    root = _repository(tmp_path)
    (root / "alpha.py").write_text("first dirty value\n", encoding="utf-8")
    first = snapshot(root)
    (root / "alpha.py").write_text("second dirty value\n", encoding="utf-8")
    second = snapshot(root)

    assert first.dirty_paths == second.dirty_paths
    assert first.dirty_hash != second.dirty_hash


def test_dirty_paths_suppress_only_persistent_symbol_hits(tmp_path: Path) -> None:
    from runtime.search.git_state import snapshot

    root = _repository(tmp_path)
    (root / "alpha.py").write_text(
        "def current_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    current = snapshot(root)
    persistent = RepositorySearchHit(
        backend="ctags",
        repository_id="active",
        path="alpha.py",
        start_line=1,
        end_line=1,
        score=130.0,
        match_kind="symbol",
        reason="persistent Ctags exact symbol definition",
    )
    live = RepositorySearchHit(
        backend="ctags",
        repository_id="active",
        path="alpha.py",
        start_line=1,
        end_line=1,
        score=130.0,
        match_kind="symbol",
        reason="current-worktree Ctags exact symbol definition",
    )

    hits = RepositorySearchEngine._suppress_stale([persistent, live], current)

    assert hits == [live]


def test_repository_path_policy_rejects_protected_and_escaping_paths(
    tmp_path: Path,
) -> None:
    from runtime.search.path_policy import (
        normalize_repository_path,
        safe_repository_path,
    )

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    assert normalize_repository_path(".gitignore") == ".gitignore"
    assert normalize_repository_path("./runtime/search/engine.py") == (
        "runtime/search/engine.py"
    )
    assert normalize_repository_path(".git/config") is None
    assert normalize_repository_path(".local-coder/holdout/secret.json") is None
    assert normalize_repository_path("evaluation/oracles/answers.json") is None
    assert normalize_repository_path("../outside.txt") is None
    assert normalize_repository_path("C:/outside.txt") is None
    assert safe_repository_path(root, "escape") is None


def test_search_request_rejects_unbounded_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bounded input"):
        RepositorySearchRequest(
            query="x" * 1001,
            repository_ids=("active",),
            worktree=tmp_path,
        )
    with pytest.raises(ValueError, match="path globs"):
        RepositorySearchRequest(
            query="value",
            repository_ids=("active",),
            worktree=tmp_path,
            path_globs=("bad\npattern",),
        )


def test_protected_paths_are_not_searched_or_reported_dirty(tmp_path: Path) -> None:
    from runtime.search.git_state import snapshot

    root = _repository(tmp_path)
    protected = root / "evaluation" / "oracles" / "answer.py"
    protected.parent.mkdir(parents=True)
    protected.write_text("protected_unique_value = 1\n", encoding="utf-8")
    backend = RipgrepBackend(load_search_profile(ROOT))
    if not backend.available:
        pytest.skip("ripgrep is not installed")

    hits = backend.search(
        RepositorySearchRequest(
            query="protected_unique_value",
            repository_ids=("active",),
            worktree=root,
            mode="text",
        ),
        repository_id="active",
    )
    current = snapshot(root)

    assert hits == []
    assert "evaluation/oracles/answer.py" not in current.untracked
