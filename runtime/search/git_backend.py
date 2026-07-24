"""Bounded read-only Git fallback and changed-path search."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

from .contracts import RepositorySearchHit, RepositorySearchRequest
from .git_state import GitSnapshot, snapshot
from .path_policy import safe_repository_path


def _paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    values: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        value = raw.decode("utf-8", errors="surrogateescape")
        resolved = safe_repository_path(root, value)
        if resolved is None:
            continue
        values.append(resolved[0])
    return values


class GitFallbackBackend:
    """Last-resort bounded scanner used only when ripgrep is unavailable."""

    def search(
        self,
        request: RepositorySearchRequest,
        *,
        repository_id: str,
        git_snapshot: GitSnapshot | None = None,
    ) -> list[RepositorySearchHit]:
        current = git_snapshot or snapshot(request.worktree)
        if request.mode == "changed":
            return self.changed_hits(
                current,
                repository_id=repository_id,
                limit=request.limit,
            )
        candidates = _paths(request.worktree)
        if request.path_globs:
            candidates = [
                path
                for path in candidates
                if any(fnmatch.fnmatch(path, glob) for glob in request.path_globs)
            ]
        query = request.query
        hits: list[RepositorySearchHit] = []
        if request.mode == "filename":
            match_all = query.casefold() in {".", "*"}
            for relative in candidates:
                if not match_all and query.casefold() not in relative.casefold():
                    continue
                hits.append(
                    RepositorySearchHit(
                        backend="git",
                        repository_id=repository_id,
                        path=relative,
                        start_line=1,
                        end_line=1,
                        score=50.0,
                        match_kind="filename",
                        reason="bounded Git filename fallback",
                    )
                )
                if len(hits) >= request.limit:
                    break
            return hits

        try:
            expression = re.compile(query, re.I) if request.mode == "regex" else None
        except re.error as exc:
            raise ValueError(
                f"Invalid repository search regular expression: {exc}"
            ) from exc
        folded = query.casefold()
        for relative in candidates:
            path = request.worktree / relative
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, start=1):
                matched = (
                    bool(expression.search(line))
                    if expression
                    else folded in line.casefold()
                )
                if not matched:
                    continue
                hits.append(
                    RepositorySearchHit(
                        backend="git",
                        repository_id=repository_id,
                        path=relative,
                        start_line=number,
                        end_line=number,
                        score=40.0,
                        match_kind="regex" if expression else "text",
                        reason="bounded Git/Python fallback",
                        snippet=line,
                    )
                )
                if len(hits) >= request.limit:
                    return hits
        return hits

    @staticmethod
    def changed_hits(
        current: GitSnapshot,
        *,
        repository_id: str,
        limit: int,
    ) -> list[RepositorySearchHit]:
        hits: list[RepositorySearchHit] = []
        categories = (
            (current.modified, "modified"),
            (current.untracked, "untracked"),
            (current.renamed, "renamed"),
            (current.deleted, "deleted"),
        )
        for paths, kind in categories:
            for path in sorted(paths):
                hits.append(
                    RepositorySearchHit(
                        backend="git",
                        repository_id=repository_id,
                        path=path,
                        start_line=None if kind == "deleted" else 1,
                        end_line=None if kind == "deleted" else 1,
                        score=120.0,
                        match_kind=kind,
                        reason=f"Git reports current path as {kind}",
                    )
                )
                if len(hits) >= limit:
                    return hits
        return hits
