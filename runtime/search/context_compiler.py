"""Compile ranked search hits into authoritative bounded source ranges."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Iterable

from .contracts import (
    QueryCandidate,
    RepositoryContextPack,
    RepositoryContextRange,
    RepositorySearchHit,
    RepositorySearchRequest,
)
from .ctags_backend import CtagsBackend
from .engine import RepositorySearchEngine
from .git_state import GitStateError, git_output, snapshot
from .index_manager import IndexManager
from .path_policy import safe_repository_path
from .policies import RepositoryContextPolicy, context_policy
from .profile import load_search_profile
from .query_router import build_query_plan
from .registry import RepositoryRecord, RepositoryRegistry
from .ripgrep_backend import RipgrepBackend
from .zoekt_backend import ZoektBackend

_SOURCE_SUFFIXES = {
    ".py",
    ".rs",
    ".go",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}


class RepositoryContextError(RuntimeError):
    """Raised when repository context cannot be compiled safely."""


def _safe_current_path(root: Path, relative: str) -> Path | None:
    resolved = safe_repository_path(root, relative)
    return resolved[1] if resolved is not None else None


def _rank(
    hit: RepositorySearchHit,
    query: QueryCandidate,
    *,
    dirty_paths: frozenset[str],
    active_repository_id: str,
) -> RepositorySearchHit:
    score = hit.score + query.weight
    reasons = [hit.reason, query.reason]
    name = Path(hit.path).name.casefold()
    query_name = Path(query.query).name.casefold()
    if name == query_name:
        score += 40.0
        reasons.append("exact filename")
    elif query_name and query_name in hit.path.casefold():
        score += 12.0
        reasons.append("path contains task term")
    if hit.match_kind == "symbol":
        score += 22.0
        reasons.append("symbol definition")
    if hit.path in dirty_paths:
        score += 30.0
        reasons.append("current changed path")
    if hit.repository_id == active_repository_id:
        score += 6.0
        reasons.append("active repository")
    suffix = Path(hit.path).suffix.casefold()
    if suffix in _SOURCE_SUFFIXES:
        score += 8.0
        reasons.append("source/configuration file")
    if hit.path.startswith("tests/") or Path(hit.path).name.startswith("test_"):
        score += 5.0
        reasons.append("test coverage")
    generated_parts = {"build", "dist", "vendor", "fixtures", "generated"}
    if any(part in generated_parts for part in PurePosixPath(hit.path).parts):
        score -= 12.0
    return replace(hit, score=score, reason="; ".join(dict.fromkeys(reasons)))


def _range_for_hit(
    hit: RepositorySearchHit,
    *,
    root: Path,
    policy: RepositoryContextPolicy,
    remaining: int,
    maximum_file_bytes: int,
) -> RepositoryContextRange | None:
    path = _safe_current_path(root, hit.path)
    if path is None or not path.is_file() or hit.match_kind == "deleted":
        return None
    try:
        if path.stat().st_size > maximum_file_bytes:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None
    anchor = max(1, min(hit.start_line or 1, len(lines)))
    start = max(1, anchor - policy.before_lines)
    end = min(len(lines), anchor + policy.after_lines)
    selected = lines[start - 1 : end]
    content = "\n".join(
        f"{number}: {line}" for number, line in enumerate(selected, start=start)
    )
    if len(content) > remaining:
        clipped = content[: max(0, remaining)]
        if "\n" in clipped:
            clipped = clipped.rsplit("\n", 1)[0]
        content = clipped
        end = start + max(0, content.count("\n"))
    if not content:
        return None
    return RepositoryContextRange(
        repository_id=hit.repository_id,
        path=hit.path,
        start_line=start,
        end_line=end,
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        reasons=tuple(
            dict.fromkeys(
                part.strip() for part in hit.reason.split(";") if part.strip()
            )
        ),
    )


class RepositoryContextCompiler:
    """Build deterministic search plans and current source context for agents."""

    def __init__(
        self,
        repository: Path,
        *,
        registry: RepositoryRegistry | None = None,
    ) -> None:
        self.repository = repository.resolve()
        profile = load_search_profile(self.repository)
        self.registry = registry or RepositoryRegistry()
        ripgrep = RipgrepBackend(profile)
        zoekt = ZoektBackend(profile)
        ctags = CtagsBackend(profile)
        indexes = IndexManager(self.registry, zoekt, ctags)
        self.engine = RepositorySearchEngine(
            registry=self.registry,
            ripgrep=ripgrep,
            zoekt=zoekt,
            ctags=ctags,
            indexes=indexes,
        )
        self.profile = profile

    @staticmethod
    def _git_common_directory(repository: Path) -> Path:
        value = Path(git_output(repository, "rev-parse", "--git-common-dir"))
        if not value.is_absolute():
            value = repository / value
        return value.resolve()

    def _active_record(self, worktree: Path) -> RepositoryRecord:
        record = self.registry.find_path(worktree)
        if record is not None:
            return record
        # Linked Git worktrees share a common Git directory with their base
        # repository. Commit hashes alone are not repository identities.
        try:
            worktree_common = self._git_common_directory(worktree)
        except (OSError, GitStateError):
            return self.registry.transient(worktree, repository_id="_active")
        for candidate in self.registry.list():
            try:
                if (
                    self._git_common_directory(candidate.resolved_path)
                    == worktree_common
                ):
                    return replace(candidate, path=str(worktree.resolve()))
            except (OSError, GitStateError):
                continue
        return self.registry.transient(worktree, repository_id="_active")

    def compile(
        self,
        task: str,
        *,
        role: str,
        worktree: Path,
        attached_repository_ids: Iterable[str] = (),
    ) -> RepositoryContextPack:
        """Compile a bounded context pack from current repository bytes."""
        started = time.perf_counter()
        policy = context_policy(role)
        worktree = worktree.resolve()
        current = snapshot(worktree)
        active = self._active_record(worktree)
        attached = tuple(dict.fromkeys(attached_repository_ids))
        repository_ids = tuple(dict.fromkeys((active.id, *attached)))
        repository_roots = {active.id: worktree}
        repository_snapshots = {active.id: current}
        failures: list[str] = []
        for repository_id in repository_ids:
            if repository_id == active.id:
                continue
            try:
                record = self.registry.get(repository_id)
                if not record.search_enabled:
                    raise RepositoryContextError(
                        f"Repository search is disabled: {repository_id}"
                    )
                repository_roots[repository_id] = record.resolved_path
                repository_snapshots[repository_id] = snapshot(record.resolved_path)
            except (OSError, GitStateError, RuntimeError) as exc:
                failures.append(f"repository:{repository_id}: {exc}")

        searchable_ids = tuple(
            repository_id
            for repository_id in repository_ids
            if repository_id in repository_roots
        )
        plan = list(build_query_plan(task, maximum=policy.maximum_queries))
        if policy.include_changed_query and not any(
            item.mode == "changed" for item in plan
        ):
            changed_query = QueryCandidate(
                query="changed files",
                mode="changed",
                reason=f"{role} policy includes current changes",
                weight=4.0,
            )
            plan = [*plan[: policy.maximum_queries - 1], changed_query]
        else:
            plan = plan[: policy.maximum_queries]
        ranked: list[RepositorySearchHit] = []
        resolved_queries: set[str] = set()
        for candidate in plan:
            request = RepositorySearchRequest(
                query=candidate.query,
                repository_ids=searchable_ids,
                worktree=worktree,
                mode=candidate.mode,
                path_globs=candidate.path_globs,
                limit=max(policy.maximum_ranges * 3, 20),
                timeout_seconds=self.profile.timeout_seconds,
                active_repository_id=active.id,
            )
            try:
                hits = self.engine.search(request)
            except Exception as exc:
                failures.append(f"{candidate.mode}:{candidate.query}: {exc}")
                continue
            failures.extend(self.engine.failures)
            if hits:
                resolved_queries.add(candidate.query)
            ranked.extend(
                _rank(
                    hit,
                    candidate,
                    dirty_paths=repository_snapshots[hit.repository_id].dirty_paths,
                    active_repository_id=active.id,
                )
                for hit in hits
                if hit.repository_id in repository_snapshots
            )

        ranked.sort(
            key=lambda hit: (
                -hit.score,
                hit.repository_id,
                hit.path,
                hit.start_line or 0,
            )
        )
        ranges: list[RepositoryContextRange] = []
        seen: set[tuple[str, str, int, int]] = set()
        per_path: Counter[tuple[str, str]] = Counter()
        remaining = min(
            policy.context_characters,
            self.profile.context_character_budget,
        )
        truncated = False
        for hit in ranked:
            if len(ranges) >= policy.maximum_ranges or remaining <= 0:
                truncated = True
                break
            path_key = (hit.repository_id, hit.path)
            if per_path[path_key] >= policy.per_file_ranges:
                continue
            source_range = _range_for_hit(
                hit,
                root=repository_roots[hit.repository_id],
                policy=policy,
                remaining=remaining,
                maximum_file_bytes=self.profile.max_file_bytes,
            )
            if source_range is None:
                continue
            key = (
                source_range.repository_id,
                source_range.path,
                source_range.start_line,
                source_range.end_line,
            )
            if key in seen:
                continue
            seen.add(key)
            ranges.append(source_range)
            per_path[path_key] += 1
            remaining -= len(source_range.content)

        # A repository map remains the final degraded fallback, but is clipped
        # and ranked last.
        if not ranges:
            try:
                fallback_request = RepositorySearchRequest(
                    query=".",
                    repository_ids=(active.id,),
                    worktree=worktree,
                    mode="filename",
                    limit=policy.maximum_ranges,
                    timeout_seconds=self.profile.timeout_seconds,
                    active_repository_id=active.id,
                )
                filenames = self.engine.search(fallback_request)
            except Exception as exc:
                failures.append(f"filename fallback: {exc}")
                filenames = []
            for hit in filenames:
                source_range = _range_for_hit(
                    replace(hit, reason="degraded filename fallback"),
                    root=worktree,
                    policy=policy,
                    remaining=remaining,
                    maximum_file_bytes=self.profile.max_file_bytes,
                )
                if source_range is None:
                    continue
                ranges.append(source_range)
                remaining -= len(source_range.content)
                if len(ranges) >= policy.maximum_ranges or remaining <= 0:
                    truncated = remaining <= 0
                    break

        unresolved = tuple(
            candidate.query
            for candidate in plan
            if candidate.mode != "changed" and candidate.query not in resolved_queries
        )
        selected_paths = tuple(dict.fromkeys(item.path for item in ranges))
        repository_states = {
            repository_id: {
                "head": state.head,
                "tree": state.tree,
                "dirty_hash": state.dirty_hash,
            }
            for repository_id, state in repository_snapshots.items()
        }
        unique_failures = tuple(dict.fromkeys(failures))
        return RepositoryContextPack(
            base_commit=current.head,
            worktree_diff_sha256=current.dirty_hash,
            active_repository_id=active.id,
            queries=tuple(candidate.query for candidate in plan),
            ranges=tuple(ranges),
            unresolved_terms=unresolved,
            truncated=truncated,
            selected_paths=selected_paths,
            backend_failures=unique_failures,
            timings_ms={"total": (time.perf_counter() - started) * 1000},
            query_plan=tuple(plan),
            repository_states=repository_states,
            backend_versions=self.engine.backend_versions(),
            degraded=bool(unique_failures or not ranges),
        )
