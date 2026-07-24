"""Persistent committed-state search through wrapped Zoekt CLI commands."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contracts import RepositorySearchHit, RepositorySearchRequest
from .path_policy import normalize_repository_path
from .profile import SearchProfile
from .registry import RepositoryRecord


class ZoektError(RuntimeError):
    """Raised when Zoekt indexing or search fails."""


def _literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _query(request: RepositorySearchRequest) -> tuple[list[str], str]:
    value = request.query.strip()
    flags: list[str] = []
    if request.mode == "filename":
        query = f"file:{_literal(value)}"
    elif request.mode == "symbol":
        flags.append("-sym")
        query = _literal(value)
    elif request.mode == "regex":
        query = value
    elif request.mode == "auto":
        terms = [term for term in re.findall(r"[A-Za-z0-9_.-]+", value) if term]
        query = " ".join(_literal(term) for term in terms[:8]) or _literal(value)
    else:
        query = _literal(value)
    for glob in request.path_globs:
        cleaned = glob.replace("*", "").strip("/")
        if cleaned:
            query += f" file:{_literal(cleaned)}"
    return flags, query


def _safe_path(record: RepositoryRecord, value: str) -> str | None:
    normalized = value.replace("\\", "/")
    if normalized.startswith(record.id + "/"):
        normalized = normalized[len(record.id) + 1 :]
    return normalize_repository_path(normalized)


class ZoektBackend:
    """Build and query isolated per-repository Zoekt indexes."""

    def __init__(self, profile: SearchProfile) -> None:
        self.profile = profile

    @property
    def available(self) -> bool:
        return all(
            shutil.which(binary) is not None
            for binary in (self.profile.zoekt_binary, self.profile.zoekt_index_binary)
        )

    def version(self) -> str | None:
        if not self.available:
            return None
        result = subprocess.run(
            [self.profile.zoekt_binary, "-h"],
            text=True,
            capture_output=True,
            check=False,
        )
        return "Zoekt CLI available" if result.returncode in {0, 2} else None

    def build(
        self,
        record: RepositoryRecord,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Rebuild one disposable committed-state index."""
        if not self.available:
            raise ZoektError("Zoekt CLI is not installed")
        if not record.zoekt_index:
            raise ZoektError("Repository has no Zoekt index path")
        index = Path(record.zoekt_index)
        index.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [
                    self.profile.zoekt_index_binary,
                    "-require_ctags=false",
                    "-index",
                    str(index),
                    str(record.resolved_path),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ZoektError("Zoekt index build timed out") from exc
        if result.returncode != 0:
            raise ZoektError(result.stderr.strip() or "Zoekt index build failed")

    def validate(
        self,
        record: RepositoryRecord,
        *,
        timeout_seconds: float = 5.0,
    ) -> bool:
        """Return whether Zoekt can open and query one derived index."""
        if not self.available or not record.zoekt_index:
            return False
        index = Path(record.zoekt_index)
        if not index.is_dir():
            return False
        try:
            result = subprocess.run(
                [
                    self.profile.zoekt_binary,
                    "-index_dir",
                    str(index),
                    "-jsonl",
                    "__local_coder_index_probe_never_matches__",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode in {0, 1}

    def search(
        self,
        request: RepositorySearchRequest,
        *,
        record: RepositoryRecord,
    ) -> list[RepositorySearchHit]:
        if not self.available:
            raise ZoektError("Zoekt CLI is not installed")
        if not record.zoekt_index or not Path(record.zoekt_index).is_dir():
            raise ZoektError("Zoekt index is unavailable")
        flags, query = _query(request)
        args = [
            self.profile.zoekt_binary,
            "-index_dir",
            record.zoekt_index,
            "-jsonl",
            *flags,
            query,
        ]
        try:
            result = subprocess.run(
                args,
                text=True,
                capture_output=True,
                check=False,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ZoektError("Zoekt search timed out") from exc
        if result.returncode != 0:
            raise ZoektError(result.stderr.strip() or "Zoekt search failed")
        hits: list[RepositorySearchHit] = []
        for line in result.stdout.splitlines():
            try:
                item: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            relative = _safe_path(
                record,
                str(item.get("FileName") or item.get("file_name") or ""),
            )
            if relative is None:
                continue
            line_matches = item.get("LineMatches") or item.get("line_matches") or []
            if not line_matches:
                hits.append(
                    RepositorySearchHit(
                        backend="zoekt",
                        repository_id=record.id,
                        path=relative,
                        start_line=1,
                        end_line=1,
                        score=70.0,
                        match_kind="filename",
                        reason="persistent committed-state Zoekt filename match",
                    )
                )
            for match in line_matches:
                if not isinstance(match, dict):
                    continue
                number = int(match.get("LineNumber") or match.get("line_number") or 1)
                raw_line = match.get("Line") or match.get("line") or ""
                if isinstance(raw_line, str):
                    snippet = raw_line.rstrip("\r\n")
                elif isinstance(raw_line, list):
                    snippet = (
                        bytes(raw_line)
                        .decode(
                            "utf-8",
                            errors="replace",
                        )
                        .rstrip("\r\n")
                    )
                else:
                    snippet = str(raw_line)
                hits.append(
                    RepositorySearchHit(
                        backend="zoekt",
                        repository_id=record.id,
                        path=relative,
                        start_line=number,
                        end_line=number,
                        score=float(item.get("Score") or item.get("score") or 65.0),
                        match_kind=(
                            "symbol" if request.mode == "symbol" else request.mode
                        ),
                        reason="persistent committed-state Zoekt match",
                        snippet=snippet,
                    )
                )
                if len(hits) >= request.limit:
                    return hits
        return hits[: request.limit]
