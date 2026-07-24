from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from runtime.search.index_manager import IndexManager
from runtime.search.registry import RepositoryRegistry


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=path,
        check=True,
    )
    (path / "module.py").write_text("def indexed_symbol():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


class _UnavailableZoekt:
    available = False


class _CtagsProbe:
    available = True

    def __init__(self) -> None:
        self.build_calls = 0
        self.refresh_calls = 0

    def validate(self, record) -> bool:
        return Path(record.ctags_index).is_file()

    def build(self, record) -> None:
        self.build_calls += 1
        Path(record.ctags_index).write_text("valid\n", encoding="utf-8")

    def refresh_paths(self, record, *, changed, deleted) -> None:
        self.refresh_calls += 1


def test_refresh_does_not_rebuild_valid_ctags_when_zoekt_is_unavailable(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repo")
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="fixture")
    ctags = _CtagsProbe()
    manager = IndexManager(registry, _UnavailableZoekt(), ctags)

    first = manager.refresh("fixture")
    second = manager.refresh("fixture")

    assert first["refreshed"] == ["ctags-rebuild"]
    assert second["refreshed"] == []
    assert ctags.build_calls == 1
    assert ctags.refresh_calls == 0
    assert second["failures"] == ["zoekt: unavailable"]
    assert registry.get("fixture").last_indexed_commit == second["head"]


def test_ctags_only_refresh_rebuilds_after_commit_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="fixture")
    ctags = _CtagsProbe()
    manager = IndexManager(registry, _UnavailableZoekt(), ctags)
    manager.refresh("fixture")

    (repository / "module.py").write_text(
        "def replacement_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "replace symbol"],
        cwd=repository,
        check=True,
    )
    payload = manager.refresh("fixture")

    assert payload["refreshed"] == ["ctags-rebuild"]
    assert ctags.build_calls == 2
    assert registry.get("fixture").last_indexed_commit == payload["head"]


def test_query_time_reconciliation_is_cached_per_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="fixture")
    ctags = _CtagsProbe()
    manager = IndexManager(registry, _UnavailableZoekt(), ctags)

    first = manager.ensure_current("fixture")
    second = manager.ensure_current("fixture")

    assert first == second
    assert ctags.build_calls == 1


def test_missing_ctags_cache_is_deferred_on_dirty_worktree(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    registry = RepositoryRegistry(tmp_path / "search-state")
    record = registry.add(repository, repository_id="fixture")
    ctags = _CtagsProbe()
    manager = IndexManager(registry, _UnavailableZoekt(), ctags)
    manager.refresh("fixture")
    Path(record.ctags_index).unlink()
    (repository / "module.py").write_text(
        "def dirty_symbol():\n    return 3\n",
        encoding="utf-8",
    )

    payload = manager.refresh("fixture")

    assert payload["refreshed"] == []
    assert ctags.build_calls == 1
    assert (
        "ctags: persistent rebuild deferred while worktree is dirty"
        in payload["failures"]
    )


class _InvalidZoekt:
    available = True

    def validate(self, record) -> bool:
        return False


def test_status_does_not_report_corrupt_zoekt_directory_as_current(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repo")
    registry = RepositoryRegistry(tmp_path / "search-state")
    record = registry.add(repository, repository_id="fixture")
    Path(record.zoekt_index).mkdir(parents=True)
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    registry.update(replace(record, last_indexed_commit=current))
    manager = IndexManager(registry, _InvalidZoekt(), _CtagsProbe())

    status = manager.status("fixture")[0]

    assert status["zoekt_index_exists"] is True
    assert status["zoekt_index_valid"] is False
    assert status["current"] is False
