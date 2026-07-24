"""External repository registry and host-side repository discovery."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .git_state import GitStateError, repository_remote, repository_root
from .profile import SearchProfile, search_data_root

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class RegistryError(RuntimeError):
    """Raised when repository registration or discovery is invalid."""


@dataclass(frozen=True)
class RepositoryRecord:
    """Repository search capability independent from edit authority."""

    id: str
    path: str
    search_enabled: bool = True
    symbol_enabled: bool = True
    edit_enabled: bool = False
    remote: str | None = None
    last_indexed_commit: str | None = None
    last_indexed_tree: str | None = None
    zoekt_index: str | None = None
    ctags_index: str | None = None

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


class RepositoryRegistry:
    """Atomic JSON registry stored outside source repositories."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or search_data_root()).resolve()
        self.path = self.root / "registry.json"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("zoekt", "ctags", "locks", "status"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _validate_record(self, record: RepositoryRecord) -> RepositoryRecord:
        """Reject registry entries that could widen derived-state authority."""
        if not _ID.fullmatch(record.id):
            raise RegistryError(f"Unsafe repository ID in registry: {record.id!r}")
        if not Path(record.path).expanduser().is_absolute():
            raise RegistryError(f"Repository path must be absolute: {record.id}")
        if not all(
            isinstance(value, bool)
            for value in (
                record.search_enabled,
                record.symbol_enabled,
                record.edit_enabled,
            )
        ):
            raise RegistryError(f"Repository capabilities are invalid: {record.id}")
        if record.edit_enabled:
            raise RegistryError(
                f"Repository registry cannot grant edit authority: {record.id}"
            )
        expected_zoekt = str((self.root / "zoekt" / record.id).resolve())
        expected_ctags = str((self.root / "ctags" / f"{record.id}.jsonl").resolve())
        if record.zoekt_index != expected_zoekt:
            raise RegistryError(
                f"Repository Zoekt path is outside managed state: {record.id}"
            )
        if record.ctags_index != expected_ctags:
            raise RegistryError(
                f"Repository Ctags path is outside managed state: {record.id}"
            )
        return record

    def _read(self) -> dict[str, RepositoryRecord]:
        if not self.path.exists():
            return {}
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"Could not read repository registry: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise RegistryError("Unsupported repository registry schema.")
        records = raw.get("repositories")
        if not isinstance(records, list):
            raise RegistryError("Repository registry entries are invalid.")
        parsed: dict[str, RepositoryRecord] = {}
        for item in records:
            if not isinstance(item, dict):
                raise RegistryError("Repository registry entry is invalid.")
            try:
                record = self._validate_record(RepositoryRecord(**item))
            except TypeError as exc:
                raise RegistryError("Repository registry entry is invalid.") from exc
            if record.id in parsed:
                raise RegistryError(f"Duplicate repository ID: {record.id}")
            parsed[record.id] = record
        return parsed

    def _write(self, records: Iterable[RepositoryRecord]) -> None:
        validated = [self._validate_record(item) for item in records]
        payload = {
            "schema_version": 1,
            "repositories": [
                asdict(item) for item in sorted(validated, key=lambda row: row.id)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".registry-",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self) -> tuple[RepositoryRecord, ...]:
        """Return all registered repositories sorted by ID."""
        return tuple(sorted(self._read().values(), key=lambda item: item.id))

    def get(self, repository_id: str) -> RepositoryRecord:
        """Return one registered repository or fail closed."""
        try:
            return self._read()[repository_id]
        except KeyError as exc:
            raise RegistryError(f"Unknown repository ID: {repository_id}") from exc

    def find_path(self, path: Path) -> RepositoryRecord | None:
        """Return the record matching one active worktree root."""
        resolved = path.resolve()
        for record in self._read().values():
            try:
                candidate = record.resolved_path
            except OSError:
                continue
            if candidate == resolved:
                return record
        return None

    def add(self, path: Path, *, repository_id: str | None = None) -> RepositoryRecord:
        """Register a Git repository for read-only search and symbol indexing."""
        try:
            resolved = repository_root(path.resolve())
        except (OSError, GitStateError) as exc:
            raise RegistryError(f"Not a readable Git repository: {path}") from exc
        identifier = repository_id or resolved.name
        if not _ID.fullmatch(identifier):
            raise RegistryError("Repository ID must be 1-80 safe filename characters.")
        records = self._read()
        duplicate = next(
            (record for record in records.values() if record.resolved_path == resolved),
            None,
        )
        if duplicate is not None and duplicate.id != identifier:
            raise RegistryError(
                f"Repository path is already registered as {duplicate.id}."
            )
        if identifier in records:
            if records[identifier].resolved_path != resolved:
                raise RegistryError(f"Repository ID is already in use: {identifier}")
            return records[identifier]
        record = RepositoryRecord(
            id=identifier,
            path=str(resolved),
            remote=repository_remote(resolved),
            zoekt_index=str(self.root / "zoekt" / identifier),
            ctags_index=str(self.root / "ctags" / f"{identifier}.jsonl"),
        )
        records[identifier] = record
        self._write(records.values())
        return record

    def update(self, record: RepositoryRecord) -> RepositoryRecord:
        """Replace one existing record atomically."""
        record = self._validate_record(record)
        records = self._read()
        if record.id not in records:
            raise RegistryError(f"Unknown repository ID: {record.id}")
        records[record.id] = record
        self._write(records.values())
        return record

    def remove(self, repository_id: str) -> RepositoryRecord:
        """Remove registration without deleting source or derived indexes."""
        records = self._read()
        try:
            removed = records.pop(repository_id)
        except KeyError as exc:
            raise RegistryError(f"Unknown repository ID: {repository_id}") from exc
        self._write(records.values())
        return removed

    def transient(
        self,
        path: Path,
        *,
        repository_id: str = "active",
    ) -> RepositoryRecord:
        """Create a non-persisted search-only active-worktree record."""
        resolved = repository_root(path.resolve())
        return RepositoryRecord(
            id=repository_id,
            path=str(resolved),
            search_enabled=True,
            symbol_enabled=False,
            edit_enabled=False,
            remote=repository_remote(resolved),
        )


def discover_repositories(
    roots: Iterable[Path],
    *,
    maximum_depth: int = 5,
    limit: int = 100,
) -> tuple[Path, ...]:
    """Find Git repositories under explicit roots without registering them."""
    found: list[Path] = []
    for root in roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for current, directories, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            directories[:] = [
                name
                for name in directories
                if name not in {".venv", "node_modules", "build", "dist", ".cache"}
            ]
            if ".git" in directories or ".git" in files:
                found.append(current_path)
                directories[:] = []
                if len(found) >= limit:
                    return tuple(found)
                continue
            if depth >= maximum_depth:
                directories[:] = []
    return tuple(found)


def locate_registered_filename(
    filename: str,
    *,
    registry: RepositoryRegistry,
    profile: SearchProfile,
    limit: int = 50,
) -> tuple[dict[str, str], ...]:
    """Use plocate for metadata discovery, retaining only registered repositories."""
    binary = shutil.which(profile.locate_binary)
    if binary is None:
        raise RegistryError("plocate is not installed")
    result = subprocess.run(
        [binary, "--basename", "--limit", str(limit * 10), filename],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode not in {0, 1}:
        raise RegistryError(result.stderr.strip() or "plocate lookup failed")
    records = registry.list()
    matches: list[dict[str, str]] = []
    for raw in result.stdout.splitlines():
        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        for record in records:
            repository = record.resolved_path
            if repository in resolved.parents or resolved == repository:
                matches.append(
                    {
                        "repository_id": record.id,
                        "path": resolved.relative_to(repository).as_posix(),
                    }
                )
                break
        if len(matches) >= limit:
            break
    return tuple(matches)
