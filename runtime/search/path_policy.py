"""Repository-relative path normalization and protected-prefix policy."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

_RESTRICTED_ROOTS = (
    ".git",
    ".local-coder/holdout",
    ".local-coder/real-task-holdout",
    "evaluation/holdout",
    "evaluation/oracles",
)


def normalize_repository_path(value: str) -> str | None:
    """Return one safe normalized path or reject escape and protected roots."""
    if any(ord(character) < 32 for character in value):
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized == ".":
        return None
    if path.parts and path.parts[0].endswith(":"):
        return None
    if any(
        normalized == root or normalized.startswith(root + "/")
        for root in _RESTRICTED_ROOTS
    ):
        return None
    return normalized


def safe_repository_path(root: Path, value: str) -> tuple[str, Path] | None:
    """Resolve a normalized path and ensure symlinks stay inside the repository."""
    normalized = normalize_repository_path(value)
    if normalized is None:
        return None
    resolved_root = root.resolve()
    candidate = (resolved_root / normalized).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        return None
    return normalized, candidate


def protected_repository_globs() -> tuple[str, ...]:
    """Return exclusion globs applied after any caller-supplied ripgrep globs."""
    return tuple(f"!{root}/**" for root in _RESTRICTED_ROOTS)
