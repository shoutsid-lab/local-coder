"""Disposable persistent index lifecycle and Git-aware refresh."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .ctags_backend import CtagsBackend, CtagsError
from .git_state import snapshot
from .registry import RepositoryRegistry
from .zoekt_backend import ZoektBackend, ZoektError


class IndexManagerError(RuntimeError):
    """Raised when a requested index operation cannot complete."""


class _IndexLock:
    def __init__(self, path: Path, *, stale_after: float = 600.0) -> None:
        self.path = path
        self.stale_after = stale_after
        self.acquired = False

    def __enter__(self) -> "_IndexLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError as exc:
                raise IndexManagerError(
                    "Could not inspect repository index lock"
                ) from exc
            if age <= self.stale_after:
                raise IndexManagerError(
                    f"Repository index is already locked: {self.path.name}"
                )
            self.path.unlink(missing_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
        self.acquired = True
        return self

    def __exit__(self, *_: Any) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


class IndexManager:
    """Coordinate Zoekt and Ctags derived caches for registered repositories."""

    def __init__(
        self,
        registry: RepositoryRegistry,
        zoekt: ZoektBackend,
        ctags: CtagsBackend,
    ) -> None:
        self.registry = registry
        self.zoekt = zoekt
        self.ctags = ctags
        self._reconciled: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _lock(self, repository_id: str) -> _IndexLock:
        return _IndexLock(self.registry.root / "locks" / f"{repository_id}.lock")

    def _status_path(self, repository_id: str) -> Path:
        return self.registry.root / "status" / f"{repository_id}.json"

    def _write_status(self, repository_id: str, payload: dict[str, Any]) -> None:
        path = self._status_path(repository_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def build(self, repository_id: str) -> dict[str, Any]:
        """Rebuild all available persistent indexes and update Git identity."""
        record = self.registry.get(repository_id)
        if not record.search_enabled:
            raise IndexManagerError(f"Repository search is disabled: {repository_id}")
        current = snapshot(record.resolved_path)
        failures: list[str] = []
        built: list[str] = []
        started = time.perf_counter()
        with self._lock(repository_id):
            if self.zoekt.available:
                try:
                    if record.zoekt_index:
                        shutil.rmtree(record.zoekt_index, ignore_errors=True)
                    self.zoekt.build(record)
                    built.append("zoekt")
                except ZoektError as exc:
                    if record.zoekt_index:
                        shutil.rmtree(record.zoekt_index, ignore_errors=True)
                    failures.append(f"zoekt: {exc}")
            else:
                if record.last_indexed_commit != current.head and record.zoekt_index:
                    shutil.rmtree(record.zoekt_index, ignore_errors=True)
                failures.append("zoekt: unavailable")
            if record.symbol_enabled and current.dirty_paths:
                if record.last_indexed_commit != current.head and record.ctags_index:
                    Path(record.ctags_index).unlink(missing_ok=True)
                failures.append(
                    "ctags: persistent rebuild deferred while worktree is dirty"
                )
            elif record.symbol_enabled and self.ctags.available:
                try:
                    self.ctags.build(record)
                    built.append("ctags")
                except CtagsError as exc:
                    if record.ctags_index:
                        Path(record.ctags_index).unlink(missing_ok=True)
                    failures.append(f"ctags: {exc}")
            elif record.symbol_enabled:
                if record.last_indexed_commit != current.head and record.ctags_index:
                    Path(record.ctags_index).unlink(missing_ok=True)
                failures.append("ctags: unavailable")
            updated = replace(
                record,
                last_indexed_commit=(
                    current.head if built else record.last_indexed_commit
                ),
                last_indexed_tree=(current.tree if built else record.last_indexed_tree),
            )
            self.registry.update(updated)
            payload = {
                "repository_id": repository_id,
                "head": current.head,
                "tree": current.tree,
                "dirty_hash": current.dirty_hash,
                "built": built,
                "failures": failures,
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
            self._write_status(repository_id, payload)
        return payload

    def refresh(self, repository_id: str) -> dict[str, Any]:
        """Refresh stale or corrupt committed-state indexes."""
        record = self.registry.get(repository_id)
        current = snapshot(record.resolved_path)
        commit_changed = record.last_indexed_commit != current.head
        zoekt_valid = False
        if self.zoekt.available:
            try:
                zoekt_valid = self.zoekt.validate(record)
            except ZoektError:
                zoekt_valid = False
        if self.zoekt.available and (commit_changed or not zoekt_valid):
            return self.build(repository_id)

        failures: list[str] = []
        refreshed: list[str] = []
        if not self.zoekt.available:
            if commit_changed and record.zoekt_index:
                shutil.rmtree(record.zoekt_index, ignore_errors=True)
            failures.append("zoekt: unavailable")

        ctags_valid = False
        if record.symbol_enabled:
            try:
                ctags_valid = self.ctags.validate(record)
            except CtagsError:
                failures.append("ctags: corrupt cache rebuilt")
            if not self.ctags.available:
                if commit_changed and record.ctags_index:
                    Path(record.ctags_index).unlink(missing_ok=True)
                    ctags_valid = False
                failures.append("ctags: unavailable")
            elif not ctags_valid or commit_changed:
                if current.dirty_paths:
                    if record.ctags_index and (commit_changed or not ctags_valid):
                        Path(record.ctags_index).unlink(missing_ok=True)
                    ctags_valid = False
                    failures.append(
                        "ctags: persistent rebuild deferred while worktree is dirty"
                    )
                else:
                    with self._lock(repository_id):
                        try:
                            self.ctags.build(record)
                            refreshed.append("ctags-rebuild")
                            ctags_valid = True
                            record = replace(
                                record,
                                last_indexed_commit=current.head,
                                last_indexed_tree=current.tree,
                            )
                            self.registry.update(record)
                        except CtagsError as exc:
                            ctags_valid = False
                            if record.ctags_index:
                                Path(record.ctags_index).unlink(missing_ok=True)
                            failures.append(f"ctags: {exc}")

        zoekt_current = bool(
            self.zoekt.available
            and record.zoekt_index
            and zoekt_valid
            and record.last_indexed_commit == current.head
        )
        ctags_current = bool(
            record.symbol_enabled
            and ctags_valid
            and record.last_indexed_commit == current.head
        )
        payload = {
            "repository_id": repository_id,
            "head": current.head,
            "tree": current.tree,
            "dirty_hash": current.dirty_hash,
            "refreshed": refreshed,
            "failures": failures,
            "current": zoekt_current or ctags_current,
        }
        self._write_status(repository_id, payload)
        return payload

    def ensure_current(self, repository_id: str) -> dict[str, Any]:
        """Reconcile one index at query time without requiring all backends."""
        try:
            record = self.registry.get(repository_id)
            current = snapshot(record.resolved_path)
            key = (repository_id, current.head, current.dirty_hash)
            cached = self._reconciled.get(key)
            if cached is not None:
                return cached
            payload = self.refresh(repository_id)
            self._reconciled = {
                item: value
                for item, value in self._reconciled.items()
                if item[0] != repository_id
            }
            self._reconciled[key] = payload
            return payload
        except (IndexManagerError, OSError, ValueError) as exc:
            return {
                "repository_id": repository_id,
                "current": False,
                "failures": [f"index refresh: {exc}"],
            }

    def status(self, repository_id: str | None = None) -> tuple[dict[str, Any], ...]:
        """Return live registry, Git, and backend state."""
        records = (
            (self.registry.get(repository_id),)
            if repository_id is not None
            else self.registry.list()
        )
        values: list[dict[str, Any]] = []
        for record in records:
            try:
                current = snapshot(record.resolved_path)
                error = None
            except Exception as exc:  # status should report, not abort other records
                current = None
                error = str(exc)
            zoekt_index_exists = bool(
                record.zoekt_index and Path(record.zoekt_index).is_dir()
            )
            zoekt_index_valid = False
            if self.zoekt.available and zoekt_index_exists:
                try:
                    zoekt_index_valid = self.zoekt.validate(record)
                except (OSError, ZoektError):
                    zoekt_index_valid = False
            ctags_index_exists = bool(
                record.ctags_index and Path(record.ctags_index).is_file()
            )
            ctags_index_valid = False
            if ctags_index_exists:
                try:
                    ctags_index_valid = self.ctags.validate(record)
                except (OSError, CtagsError):
                    ctags_index_valid = False
            values.append(
                {
                    **asdict(record),
                    "path_exists": record.resolved_path.is_dir(),
                    "head": current.head if current else None,
                    "dirty_hash": current.dirty_hash if current else None,
                    "zoekt_available": self.zoekt.available,
                    "ctags_available": self.ctags.available,
                    "zoekt_index_exists": zoekt_index_exists,
                    "zoekt_index_valid": zoekt_index_valid,
                    "ctags_index_exists": ctags_index_exists,
                    "ctags_index_valid": ctags_index_valid,
                    "current": bool(
                        current
                        and record.last_indexed_commit == current.head
                        and (
                            zoekt_index_valid
                            or (record.symbol_enabled and ctags_index_valid)
                        )
                    ),
                    "error": error,
                }
            )
        return tuple(values)
