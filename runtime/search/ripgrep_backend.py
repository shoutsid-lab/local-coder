"""Structured current-worktree search using ripgrep JSON output."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .contracts import RepositorySearchHit, RepositorySearchRequest
from .path_policy import protected_repository_globs, safe_repository_path
from .profile import SearchProfile


class RipgrepError(RuntimeError):
    """Raised when a structured ripgrep invocation fails."""


def _safe_relative(root: Path, value: str) -> str | None:
    resolved = safe_repository_path(root, value)
    return resolved[0] if resolved is not None else None


class RipgrepBackend:
    """Search current bytes without a shell and parse machine output only."""

    def __init__(self, profile: SearchProfile) -> None:
        self.profile = profile

    @property
    def available(self) -> bool:
        return shutil.which(self.profile.ripgrep_binary) is not None

    def version(self) -> str | None:
        if not self.available:
            return None
        result = subprocess.run(
            [self.profile.ripgrep_binary, "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.splitlines()[0].strip() if result.returncode == 0 else None

    def search(
        self,
        request: RepositorySearchRequest,
        *,
        repository_id: str,
    ) -> list[RepositorySearchHit]:
        if not self.available:
            raise RipgrepError("ripgrep is not installed")
        if request.mode == "changed":
            return []
        if request.mode == "filename":
            return self._search_filenames(request, repository_id=repository_id)

        args = [
            self.profile.ripgrep_binary,
            "--json",
            "--hidden",
            "--line-number",
            "--column",
            "--no-messages",
            "--color=never",
            f"--max-filesize={self.profile.max_file_size}",
        ]
        if request.mode != "regex":
            args.append("--fixed-strings")
        args.extend(["--ignore-case", request.query])
        for glob in request.path_globs:
            args.extend(["--glob", glob])
        for glob in protected_repository_globs():
            args.extend(["--glob", glob])
        args.append(".")
        try:
            result = subprocess.run(
                args,
                cwd=request.worktree,
                text=True,
                capture_output=True,
                check=False,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RipgrepError("ripgrep search timed out") from exc
        if result.returncode not in {0, 1}:
            raise RipgrepError(result.stderr.strip() or "ripgrep search failed")

        hits: list[RepositorySearchHit] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_data = data.get("path") or {}
            relative = _safe_relative(
                request.worktree,
                str(path_data.get("text") or ""),
            )
            if relative is None:
                continue
            line_number = int(data.get("line_number") or 1)
            lines = data.get("lines") or {}
            snippet = str(lines.get("text") or "").rstrip("\r\n")
            hits.append(
                RepositorySearchHit(
                    backend="ripgrep",
                    repository_id=repository_id,
                    path=relative,
                    start_line=line_number,
                    end_line=line_number,
                    score=80.0,
                    match_kind="regex" if request.mode == "regex" else "text",
                    reason="current worktree ripgrep match",
                    snippet=snippet,
                )
            )
            if len(hits) >= request.limit:
                break
        return hits

    def _search_filenames(
        self,
        request: RepositorySearchRequest,
        *,
        repository_id: str,
    ) -> list[RepositorySearchHit]:
        args = [
            self.profile.ripgrep_binary,
            "--files",
            "--hidden",
            "--null",
        ]
        for glob in request.path_globs:
            args.extend(["--glob", glob])
        for glob in protected_repository_globs():
            args.extend(["--glob", glob])
        try:
            result = subprocess.run(
                args,
                cwd=request.worktree,
                capture_output=True,
                check=False,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RipgrepError("ripgrep filename search timed out") from exc
        if result.returncode != 0:
            raise RipgrepError(result.stderr.decode(errors="replace").strip())
        query = request.query.casefold()
        match_all = query in {".", "*"}
        hits: list[RepositorySearchHit] = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            relative = _safe_relative(
                request.worktree, raw.decode("utf-8", errors="surrogateescape")
            )
            if relative is None or (not match_all and query not in relative.casefold()):
                continue
            exact = not match_all and Path(relative).name.casefold() == query
            hits.append(
                RepositorySearchHit(
                    backend="ripgrep",
                    repository_id=repository_id,
                    path=relative,
                    start_line=1,
                    end_line=1,
                    score=110.0 if exact else 90.0,
                    match_kind="filename",
                    reason=(
                        "exact current-worktree filename"
                        if exact
                        else "current-worktree path contains query"
                    ),
                )
            )
            if len(hits) >= request.limit:
                break
        return hits
