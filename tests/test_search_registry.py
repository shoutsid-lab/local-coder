from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runtime.search.registry import (
    RegistryError,
    RepositoryRegistry,
    discover_repositories,
)


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


def test_registry_is_external_atomic_and_edit_disabled(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "source")
    registry = RepositoryRegistry(tmp_path / "search-state")
    record = registry.add(repository, repository_id="fixture")
    assert record.edit_enabled is False
    assert registry.get("fixture").resolved_path == repository.resolve()
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["repositories"][0]["zoekt_index"].endswith("zoekt/fixture")
    registry.remove("fixture")
    assert registry.list() == ()
    assert repository.is_dir()


def test_registry_rejects_duplicate_path_and_unsafe_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "source")
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="one")
    with pytest.raises(RegistryError):
        registry.add(repository, repository_id="two")
    with pytest.raises(RegistryError):
        registry.add(repository, repository_id="../unsafe")


def test_discovery_does_not_register_results(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "projects" / "one")
    registry = RepositoryRegistry(tmp_path / "search-state")
    found = discover_repositories([tmp_path / "projects"])
    assert repository in found
    assert registry.list() == ()


def test_registry_rejects_tampered_derived_state_paths(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "source")
    registry = RepositoryRegistry(tmp_path / "search-state")
    registry.add(repository, repository_id="fixture")
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    payload["repositories"][0]["ctags_index"] = str(tmp_path / "outside.jsonl")
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryError, match="outside managed state"):
        registry.list()


def test_registry_add_is_idempotent_for_same_id_and_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "source")
    registry = RepositoryRegistry(tmp_path / "search-state")
    first = registry.add(repository, repository_id="fixture")
    second = registry.add(repository, repository_id="fixture")

    assert second == first
    assert len(registry.list()) == 1
