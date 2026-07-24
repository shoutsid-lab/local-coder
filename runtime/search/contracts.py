"""Typed contracts for repository search and compiled source context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SearchMode = Literal["auto", "filename", "text", "regex", "symbol", "changed"]
SearchBackend = Literal["ripgrep", "zoekt", "ctags", "git"]


@dataclass(frozen=True)
class RepositorySearchRequest:
    """One bounded repository-scoped search request."""

    query: str
    repository_ids: tuple[str, ...]
    worktree: Path
    mode: SearchMode = "auto"
    path_globs: tuple[str, ...] = ()
    limit: int = 20
    timeout_seconds: float = 5.0
    active_repository_id: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("Repository search query must not be empty.")
        if len(self.query) > 1000 or any(
            ord(character) < 32 and character not in {"\t", "\n"}
            for character in self.query
        ):
            raise ValueError("Repository search query is outside bounded input limits.")
        if not self.repository_ids:
            raise ValueError("At least one repository ID is required.")
        if len(self.repository_ids) > 32:
            raise ValueError("At most 32 repository IDs may be searched at once.")
        if len(self.path_globs) > 32 or any(
            not glob
            or len(glob) > 240
            or any(ord(character) < 32 for character in glob)
            for glob in self.path_globs
        ):
            raise ValueError("Repository path globs are outside bounded input limits.")
        if self.limit < 1 or self.limit > 200:
            raise ValueError("Repository search limit must be between 1 and 200.")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError(
                "Repository search timeout must be between 0 and 60 seconds."
            )
        if self.mode not in {"auto", "filename", "text", "regex", "symbol", "changed"}:
            raise ValueError(f"Unsupported repository search mode: {self.mode}")


@dataclass(frozen=True)
class RepositorySearchHit:
    """One backend-neutral repository search match."""

    backend: SearchBackend
    repository_id: str
    path: str
    start_line: int | None
    end_line: int | None
    score: float
    match_kind: str
    reason: str
    snippet: str = ""
    symbol_name: str | None = None
    stale: bool = False

    def key(self) -> tuple[str, str, int | None, str]:
        """Return a stable de-duplication key."""
        return (self.repository_id, self.path, self.start_line, self.match_kind)


@dataclass(frozen=True)
class RepositoryContextRange:
    """One authoritative current-worktree source range selected for a model."""

    repository_id: str
    path: str
    start_line: int
    end_line: int
    content: str
    content_sha256: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryContextPack:
    """Bounded source ranges and lineage compiled for one role request."""

    base_commit: str
    worktree_diff_sha256: str
    queries: tuple[str, ...]
    ranges: tuple[RepositoryContextRange, ...]
    unresolved_terms: tuple[str, ...]
    truncated: bool
    selected_paths: tuple[str, ...] = ()
    backend_failures: tuple[str, ...] = ()
    timings_ms: dict[str, float] = field(default_factory=dict)
    query_plan: tuple[QueryCandidate, ...] = ()
    repository_states: dict[str, dict[str, str]] = field(default_factory=dict)
    backend_versions: dict[str, str] = field(default_factory=dict)
    degraded: bool = False
    active_repository_id: str = "_active"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for audit lineage."""
        return asdict(self)

    def evidence_blocks(self) -> list[str]:
        """Render selected ranges for the existing DSPy evidence input."""
        blocks: list[str] = []
        for source_range in self.ranges:
            reason = "; ".join(source_range.reasons)
            blocks.append(
                "\n".join(
                    (
                        f"Repository: {source_range.repository_id}",
                        (
                            f"Source: {source_range.path}:"
                            f"{source_range.start_line}-{source_range.end_line}"
                        ),
                        f"Selected because: {reason}",
                        source_range.content,
                    )
                )
            )
        if self.unresolved_terms:
            blocks.append(
                "Unresolved search terms: " + ", ".join(self.unresolved_terms)
            )
        return blocks


@dataclass(frozen=True)
class QueryCandidate:
    """One deterministic backend-neutral query derived from task text."""

    query: str
    mode: SearchMode
    reason: str
    weight: float
    path_globs: tuple[str, ...] = ()
