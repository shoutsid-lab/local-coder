"""Backend routing, live-overlay authority, and result merging."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .contracts import RepositorySearchHit, RepositorySearchRequest
from .ctags_backend import CtagsBackend, CtagsError
from .git_backend import GitFallbackBackend
from .git_state import GitSnapshot, snapshot
from .index_manager import IndexManager
from .registry import RepositoryRecord, RepositoryRegistry, RegistryError
from .ripgrep_backend import RipgrepBackend, RipgrepError
from .zoekt_backend import ZoektBackend, ZoektError

_BACKEND_PRIORITY = {"ripgrep": 4, "git": 3, "ctags": 2, "zoekt": 1}


class RepositorySearchEngine:
    """Search registered repositories while preserving current-worktree truth."""

    def __init__(
        self,
        *,
        registry: RepositoryRegistry,
        ripgrep: RipgrepBackend,
        zoekt: ZoektBackend,
        ctags: CtagsBackend,
        indexes: IndexManager,
        fallback: GitFallbackBackend | None = None,
    ) -> None:
        self.registry = registry
        self.ripgrep = ripgrep
        self.zoekt = zoekt
        self.ctags = ctags
        self.indexes = indexes
        self.fallback = fallback or GitFallbackBackend()
        self.failures: list[str] = []

    def _record_for(self, repository_id: str, worktree: Path) -> RepositoryRecord:
        try:
            record = self.registry.get(repository_id)
        except RegistryError:
            if repository_id not in {"active", "_active", worktree.name}:
                raise
            return self.registry.transient(worktree, repository_id=repository_id)
        if not record.search_enabled:
            raise RegistryError(f"Repository search is disabled: {repository_id}")
        return record

    @staticmethod
    def _suppress_stale(
        hits: Iterable[RepositorySearchHit], current: GitSnapshot
    ) -> list[RepositorySearchHit]:
        values: list[RepositorySearchHit] = []
        dirty = current.modified | current.untracked | current.renamed
        for hit in hits:
            if hit.path in current.deleted:
                continue
            if hit.backend == "zoekt" and hit.path in dirty:
                continue
            if (
                hit.backend == "ctags"
                and hit.reason.startswith("persistent Ctags")
                and hit.path in dirty
            ):
                continue
            values.append(hit)
        return values

    def backend_versions(self) -> dict[str, str]:
        """Return detected backend versions without making them mandatory."""
        return {
            "ripgrep": self.ripgrep.version() or "unavailable",
            "zoekt": self.zoekt.version() or "unavailable",
            "ctags": self.ctags.version() or "unavailable",
            "git": "git-cli",
        }

    @staticmethod
    def _merge(
        hits: Iterable[RepositorySearchHit],
        limit: int,
    ) -> list[RepositorySearchHit]:
        selected: dict[tuple[str, str, int | None, str], RepositorySearchHit] = {}
        for hit in hits:
            key = hit.key()
            current = selected.get(key)
            if current is None:
                selected[key] = hit
                continue
            current_rank = (current.score, _BACKEND_PRIORITY[current.backend])
            candidate_rank = (hit.score, _BACKEND_PRIORITY[hit.backend])
            if candidate_rank > current_rank:
                selected[key] = hit
        return sorted(
            selected.values(),
            key=lambda hit: (
                -hit.score,
                -_BACKEND_PRIORITY[hit.backend],
                hit.repository_id,
                hit.path,
                hit.start_line or 0,
            ),
        )[:limit]

    def search(self, request: RepositorySearchRequest) -> list[RepositorySearchHit]:
        """Execute one bounded request across attached repository IDs."""
        self.failures = []
        all_hits: list[RepositorySearchHit] = []
        active_repository_id = request.active_repository_id
        if active_repository_id is None:
            active = self.registry.find_path(request.worktree)
            if active is not None:
                active_repository_id = active.id
            elif "active" in request.repository_ids:
                active_repository_id = "active"

        for repository_id in dict.fromkeys(request.repository_ids):
            record = self._record_for(repository_id, request.worktree)
            worktree = (
                request.worktree
                if repository_id == active_repository_id
                else record.resolved_path
            )
            scoped = replace(
                request,
                worktree=worktree,
                repository_ids=(repository_id,),
                active_repository_id=repository_id,
            )
            current = snapshot(worktree)
            if record.zoekt_index:
                refresh = self.indexes.ensure_current(repository_id)
                self.failures.extend(str(item) for item in refresh.get("failures", []))
                record = self.registry.get(repository_id)
            if request.mode == "changed":
                all_hits.extend(
                    self.fallback.search(
                        scoped,
                        repository_id=repository_id,
                        git_snapshot=current,
                    )
                )
                continue

            persistent: list[RepositorySearchHit] = []
            if (
                request.mode == "symbol"
                and record.symbol_enabled
                and record.ctags_index
                and record.last_indexed_commit == current.head
            ):
                try:
                    persistent.extend(
                        self.ctags.search(record, request.query, limit=request.limit)
                    )
                except CtagsError as exc:
                    self.failures.append(f"{repository_id} ctags: {exc}")
            if request.mode == "symbol" and record.symbol_enabled:
                dirty_paths = sorted(
                    current.modified | current.untracked | current.renamed
                )
                if dirty_paths and self.ctags.available:
                    try:
                        persistent.extend(
                            self.ctags.search_current_paths(
                                worktree,
                                dirty_paths,
                                request.query,
                                repository_id=repository_id,
                                limit=request.limit,
                                timeout_seconds=request.timeout_seconds,
                            )
                        )
                    except CtagsError as exc:
                        self.failures.append(f"{repository_id} current ctags: {exc}")
            if (
                record.zoekt_index
                and Path(record.zoekt_index).is_dir()
                and record.last_indexed_commit == current.head
            ):
                try:
                    persistent.extend(self.zoekt.search(scoped, record=record))
                except ZoektError as exc:
                    self.failures.append(f"{repository_id} zoekt: {exc}")
            persistent = self._suppress_stale(persistent, current)

            live: list[RepositorySearchHit]
            try:
                live = self.ripgrep.search(scoped, repository_id=repository_id)
            except RipgrepError as exc:
                self.failures.append(f"{repository_id} ripgrep: {exc}")
                live = self.fallback.search(
                    scoped,
                    repository_id=repository_id,
                    git_snapshot=current,
                )
            dirty_bonus: list[RepositorySearchHit] = []
            for hit in live:
                if hit.path in current.dirty_paths:
                    dirty_bonus.append(
                        replace(
                            hit,
                            score=hit.score + 35.0,
                            reason=hit.reason + "; current dirty/untracked path",
                        )
                    )
                else:
                    dirty_bonus.append(hit)
            all_hits.extend(self._merge([*persistent, *dirty_bonus], request.limit))
        return self._merge(all_hits, request.limit)
