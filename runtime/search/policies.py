"""Role-specific bounded repository context policies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryContextPolicy:
    """Deterministic range and budget limits for one read-only role."""

    role: str
    maximum_queries: int
    maximum_ranges: int
    context_characters: int
    before_lines: int
    after_lines: int
    per_file_ranges: int
    include_changed_query: bool


_POLICIES = {
    "explorer": RepositoryContextPolicy(
        role="explorer",
        maximum_queries=10,
        maximum_ranges=12,
        context_characters=14000,
        before_lines=8,
        after_lines=18,
        per_file_ranges=2,
        include_changed_query=True,
    ),
    "planner": RepositoryContextPolicy(
        role="planner",
        maximum_queries=8,
        maximum_ranges=8,
        context_characters=18000,
        before_lines=16,
        after_lines=32,
        per_file_ranges=2,
        include_changed_query=True,
    ),
    "reviewer": RepositoryContextPolicy(
        role="reviewer",
        maximum_queries=8,
        maximum_ranges=10,
        context_characters=20000,
        before_lines=20,
        after_lines=40,
        per_file_ranges=3,
        include_changed_query=True,
    ),
}


def context_policy(role: str) -> RepositoryContextPolicy:
    """Return a frozen policy for a supported read-only role."""
    try:
        return _POLICIES[role]
    except KeyError as exc:
        raise ValueError(f"Unsupported repository context role: {role}") from exc
