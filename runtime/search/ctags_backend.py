"""Persistent Universal Ctags symbol definitions and file outlines."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .contracts import RepositorySearchHit
from .path_policy import normalize_repository_path, safe_repository_path
from .profile import SearchProfile
from .registry import RepositoryRecord


class CtagsError(RuntimeError):
    """Raised when Universal Ctags indexing fails."""


_MAX_CACHE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SymbolEntry:
    """Normalized symbol definition emitted by Universal Ctags."""

    name: str
    path: str
    line: int
    language: str | None
    kind: str | None
    scope: str | None
    pattern: str | None
    file_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "language": self.language,
            "kind": self.kind,
            "scope": self.scope,
            "pattern": self.pattern,
            "file_sha256": self.file_sha256,
        }


class CtagsBackend:
    """Build, update, and query a disposable JSONL symbol cache."""

    def __init__(self, profile: SearchProfile) -> None:
        self.profile = profile

    @property
    def available(self) -> bool:
        return shutil.which(self.profile.ctags_binary) is not None

    def version(self) -> str | None:
        if not self.available:
            return None
        result = subprocess.run(
            [self.profile.ctags_binary, "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.splitlines()[0].strip() if result.returncode == 0 else None

    @staticmethod
    def _tracked_files(root: Path) -> tuple[str, ...]:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise CtagsError(result.stderr.decode(errors="replace").strip())
        paths: list[str] = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            value = raw.decode("utf-8", errors="surrogateescape")
            resolved = safe_repository_path(root, value)
            if resolved is None:
                continue
            relative, candidate = resolved
            if candidate.is_file() and not candidate.is_symlink():
                paths.append(relative)
        return tuple(paths)

    def _generate(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> list[SymbolEntry]:
        bounded_paths: list[str] = []
        for value in dict.fromkeys(paths):
            resolved = safe_repository_path(root, value)
            if resolved is None:
                continue
            relative, candidate = resolved
            try:
                metadata = candidate.stat()
            except OSError:
                continue
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or metadata.st_size > self.profile.max_file_bytes
            ):
                continue
            bounded_paths.append(relative)
        paths = tuple(bounded_paths)
        if not paths:
            return []
        if not self.available:
            raise CtagsError("Universal Ctags is not installed")
        args = [
            self.profile.ctags_binary,
            "--options=NONE",
            "--output-format=json",
            "--fields=+nKSl",
            "--extras=+q",
            "--sort=no",
            "-f",
            "-",
            "-L",
            "-",
        ]
        try:
            result = subprocess.run(
                args,
                cwd=root,
                input="\n".join(paths) + "\n",
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CtagsError("Universal Ctags indexing timed out") from exc
        if result.returncode != 0:
            raise CtagsError(result.stderr.strip() or "Universal Ctags indexing failed")
        hashes: dict[str, str] = {}
        entries: list[SymbolEntry] = []
        for line in result.stdout.splitlines():
            try:
                item: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("_type") != "tag":
                continue
            relative = str(item.get("path") or "")
            resolved = safe_repository_path(root, relative)
            if resolved is None:
                continue
            normalized, candidate = resolved
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if normalized not in hashes:
                hashes[normalized] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            scope = item.get("scope")
            if scope is None:
                for key in ("class", "struct", "namespace", "function", "method"):
                    if item.get(key):
                        scope = f"{key}:{item[key]}"
                        break
            entries.append(
                SymbolEntry(
                    name=str(item.get("name") or ""),
                    path=normalized,
                    line=int(item.get("line") or 1),
                    language=(
                        str(item.get("language")) if item.get("language") else None
                    ),
                    kind=str(item.get("kind")) if item.get("kind") else None,
                    scope=str(scope) if scope else None,
                    pattern=str(item.get("pattern")) if item.get("pattern") else None,
                    file_sha256=hashes[normalized],
                )
            )
        return entries

    @staticmethod
    def _write(path: Path, entries: Iterable[SymbolEntry]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".ctags-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                for entry in sorted(
                    entries,
                    key=lambda item: (item.path, item.line, item.name),
                ):
                    handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _load(path: Path) -> list[SymbolEntry]:
        if not path.is_file():
            return []
        entries: list[SymbolEntry] = []
        try:
            if path.stat().st_size > _MAX_CACHE_BYTES:
                raise CtagsError("Ctags cache exceeds the bounded cache limit")
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CtagsError(f"Could not read Ctags cache: {exc}") from exc
        for line in lines:
            try:
                item = json.loads(line)
                entry = SymbolEntry(**item)
                normalized = normalize_repository_path(entry.path)
                if (
                    normalized is None
                    or normalized != entry.path
                    or not entry.name
                    or entry.line < 1
                ):
                    raise ValueError("invalid symbol entry")
                entries.append(entry)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CtagsError("Ctags cache is corrupt and must be rebuilt") from exc
        return entries

    def validate(self, record: RepositoryRecord) -> bool:
        """Return whether the current symbol cache is readable and compatible."""
        if not record.ctags_index or not Path(record.ctags_index).is_file():
            return False
        self._load(Path(record.ctags_index))
        return True

    def build(
        self,
        record: RepositoryRecord,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Rebuild all current symbol entries for one repository."""
        if not record.ctags_index:
            raise CtagsError("Repository has no Ctags index path")
        entries = self._generate(
            record.resolved_path,
            self._tracked_files(record.resolved_path),
            timeout_seconds=timeout_seconds,
        )
        self._write(Path(record.ctags_index), entries)

    def refresh_paths(
        self,
        record: RepositoryRecord,
        *,
        changed: Iterable[str],
        deleted: Iterable[str] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        """Replace symbol rows only for changed paths."""
        if not record.ctags_index:
            raise CtagsError("Repository has no Ctags index path")
        changed_set = {
            normalized
            for path in changed
            if (normalized := normalize_repository_path(path)) is not None
        }
        deleted_set = {
            normalized
            for path in deleted
            if (normalized := normalize_repository_path(path)) is not None
        }
        removed = changed_set | deleted_set
        old = [
            entry
            for entry in self._load(Path(record.ctags_index))
            if entry.path not in removed
        ]
        existing = [
            path for path in changed_set if (record.resolved_path / path).is_file()
        ]
        new = self._generate(
            record.resolved_path,
            existing,
            timeout_seconds=timeout_seconds,
        )
        self._write(Path(record.ctags_index), [*old, *new])

    def search(
        self,
        record: RepositoryRecord,
        query: str,
        *,
        limit: int = 20,
    ) -> list[RepositorySearchHit]:
        """Return exact then case-insensitive symbol definitions."""
        if not record.ctags_index:
            raise CtagsError("Repository has no Ctags index path")
        return self._search_entries(
            self._load(Path(record.ctags_index)),
            query,
            repository_id=record.id,
            reason_prefix="persistent Ctags",
            limit=limit,
        )

    def search_current_paths(
        self,
        root: Path,
        paths: Iterable[str],
        query: str,
        *,
        repository_id: str,
        limit: int = 20,
        timeout_seconds: float = 5.0,
    ) -> list[RepositorySearchHit]:
        """Generate symbol definitions only for current dirty paths."""
        entries = self._generate(
            root,
            paths,
            timeout_seconds=timeout_seconds,
        )
        return self._search_entries(
            entries,
            query,
            repository_id=repository_id,
            reason_prefix="current-worktree Ctags",
            limit=limit,
        )

    @staticmethod
    def _search_entries(
        entries: Iterable[SymbolEntry],
        query: str,
        *,
        repository_id: str,
        reason_prefix: str,
        limit: int,
    ) -> list[RepositorySearchHit]:
        entries = list(entries)
        exact = [entry for entry in entries if entry.name == query]
        folded = [
            entry
            for entry in entries
            if entry.name.casefold() == query.casefold() and entry not in exact
        ]
        partial = [
            entry
            for entry in entries
            if query.casefold() in entry.name.casefold()
            and entry not in exact
            and entry not in folded
        ]
        hits: list[RepositorySearchHit] = []
        for entry in [*exact, *folded, *partial]:
            match = (
                "exact symbol definition"
                if entry.name == query
                else "symbol definition"
            )
            scope = f" in {entry.scope}" if entry.scope else ""
            hits.append(
                RepositorySearchHit(
                    backend="ctags",
                    repository_id=repository_id,
                    path=entry.path,
                    start_line=entry.line,
                    end_line=entry.line,
                    score=130.0 if entry.name == query else 100.0,
                    match_kind="symbol",
                    reason=f"{reason_prefix} {match}{scope}",
                    snippet=entry.pattern or "",
                    symbol_name=entry.name,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def outline(
        self,
        record: RepositoryRecord,
        path: str,
        *,
        limit: int = 100,
    ) -> list[SymbolEntry]:
        """Return one file's ordered symbol outline."""
        if not record.ctags_index:
            raise CtagsError("Repository has no Ctags index path")
        normalized = normalize_repository_path(path)
        if normalized is None:
            return []
        return [
            entry
            for entry in self._load(Path(record.ctags_index))
            if entry.path == normalized
        ][:limit]
