from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from runtime.search.context_compiler import RepositoryContextCompiler, _rank
from runtime.search.contracts import QueryCandidate, RepositorySearchHit
from runtime.search.registry import RepositoryRegistry

ROOT = Path(__file__).resolve().parents[1]


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=root,
        check=True,
    )
    (root / "calculator.py").write_text(
        "def calculate_total(values):\n    return sum(values)\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "from calculator import calculate_total\n\n"
        "def test_total():\n    assert calculate_total([1, 2]) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return root


def test_context_compiler_rereads_dirty_current_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    registry = RepositoryRegistry(tmp_path / "search-state")
    compiler = RepositoryContextCompiler(ROOT, registry=registry)
    (repository / "calculator.py").write_text(
        "def calculate_total(values):\n    return sum(values) + 10  # dirty truth\n",
        encoding="utf-8",
    )
    pack = compiler.compile(
        "Fix calculate_total in calculator.py and update its test.",
        role="explorer",
        worktree=repository,
    )
    assert pack.base_commit
    assert "calculator.py" in pack.selected_paths
    assert any("dirty truth" in item.content for item in pack.ranges)
    assert all(item.content_sha256 for item in pack.ranges)
    assert len("".join(item.content for item in pack.ranges)) <= 14000


def test_planner_policy_returns_fewer_longer_ranges(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    registry = RepositoryRegistry(tmp_path / "search-state")
    compiler = RepositoryContextCompiler(ROOT, registry=registry)
    pack = compiler.compile(
        "Plan changes to calculate_total and test_calculator.py.",
        role="planner",
        worktree=repository,
    )
    assert len(pack.ranges) <= 8
    assert set(pack.selected_paths) <= {"calculator.py", "test_calculator.py"}


def test_context_compiler_keeps_same_path_from_attached_repository(
    tmp_path: Path,
) -> None:
    active_parent = tmp_path / "active-parent"
    attached_parent = tmp_path / "attached-parent"
    active_parent.mkdir()
    attached_parent.mkdir()
    active = _repository(active_parent)
    attached = _repository(attached_parent)
    (active / "calculator.py").write_text(
        "def shared_repository_symbol():\n    return 'active'\n",
        encoding="utf-8",
    )
    (attached / "calculator.py").write_text(
        "def shared_repository_symbol():\n    return 'attached'\n",
        encoding="utf-8",
    )
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(attached, repository_id="attached")
    compiler = RepositoryContextCompiler(ROOT, registry=registry)

    pack = compiler.compile(
        "Inspect shared_repository_symbol in calculator.py.",
        role="explorer",
        worktree=active,
        attached_repository_ids=("attached",),
    )

    repositories = {
        item.repository_id
        for item in pack.ranges
        if item.path == "calculator.py" and "shared_repository_symbol" in item.content
    }
    assert repositories == {"_active", "attached"}
    assert set(pack.repository_states) == {"_active", "attached"}
    assert pack.query_plan
    assert "ripgrep" in pack.backend_versions


def test_context_compiler_matches_registered_linked_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="registered")
    compiler = RepositoryContextCompiler(ROOT, registry=registry)

    record = compiler._active_record(linked)

    assert record.id == "registered"
    assert record.resolved_path == linked.resolve()


def test_context_ranking_prefers_active_repository() -> None:
    candidate = QueryCandidate(
        query="shared_repository_symbol",
        mode="symbol",
        reason="identifier from task",
        weight=8.0,
    )
    hit = RepositorySearchHit(
        backend="ctags",
        repository_id="active",
        path="calculator.py",
        start_line=1,
        end_line=1,
        score=20.0,
        match_kind="symbol",
        reason="exact symbol definition",
    )

    active = _rank(
        hit,
        candidate,
        dirty_paths=frozenset(),
        active_repository_id="active",
    )
    attached = _rank(
        replace(hit, repository_id="attached"),
        candidate,
        dirty_paths=frozenset(),
        active_repository_id="active",
    )

    assert active.score == attached.score + 6.0
    assert "active repository" in active.reason


def test_context_policy_reserves_a_changed_files_query(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    registry = RepositoryRegistry(tmp_path / "search-state")
    compiler = RepositoryContextCompiler(ROOT, registry=registry)

    pack = compiler.compile(
        "Fix AlphaOne BetaTwo GammaThree DeltaFour EpsilonFive ZetaSix "
        "EtaSeven ThetaEight IotaNine KappaTen LambdaEleven in many files.",
        role="explorer",
        worktree=repository,
    )

    assert len(pack.query_plan) <= 10
    assert pack.query_plan[-1].mode == "changed"
