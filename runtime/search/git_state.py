"""Git identity and current-worktree overlay helpers."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .path_policy import normalize_repository_path


class GitStateError(RuntimeError):
    """Raised when repository identity cannot be inspected safely."""


@dataclass(frozen=True)
class GitSnapshot:
    """Committed identity and path-level current-worktree state."""

    head: str
    tree: str
    dirty_hash: str
    modified: frozenset[str]
    deleted: frozenset[str]
    renamed: frozenset[str]
    untracked: frozenset[str]

    @property
    def dirty_paths(self) -> frozenset[str]:
        return frozenset(self.modified | self.deleted | self.renamed | self.untracked)


_PER_FILE_HASH_LIMIT = 8 * 1024 * 1024
_TOTAL_HASH_LIMIT = 32 * 1024 * 1024


def _dirty_state_hash(
    root: Path,
    status: bytes,
    *,
    paths: set[str],
    deleted: set[str],
) -> str:
    """Hash path state and bounded current bytes without loading files in memory."""
    digest = hashlib.sha256(status)
    remaining = _TOTAL_HASH_LIMIT
    resolved_root = root.resolve()
    for relative in sorted(paths):
        digest.update(b"\0path\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        if relative in deleted:
            digest.update(b"\0deleted")
            continue
        candidate = root / relative
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            digest.update(f"\0missing:{type(exc).__name__}".encode())
            continue
        digest.update(
            f"\0mode={metadata.st_mode};size={metadata.st_size};"
            f"mtime={metadata.st_mtime_ns}".encode()
        )
        if stat.S_ISLNK(metadata.st_mode):
            try:
                digest.update(os.readlink(candidate).encode("utf-8", "surrogateescape"))
            except OSError as exc:
                digest.update(f"\0unreadable-link:{type(exc).__name__}".encode())
            continue
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            digest.update(f"\0unresolved:{type(exc).__name__}".encode())
            continue
        if resolved != resolved_root and resolved_root not in resolved.parents:
            digest.update(b"\0outside-worktree")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            digest.update(b"\0non-regular")
            continue
        allowance = min(_PER_FILE_HASH_LIMIT, remaining)
        if allowance <= 0:
            digest.update(b"\0content-budget-exhausted")
            continue
        try:
            with candidate.open("rb") as handle:
                if metadata.st_size <= allowance:
                    while chunk := handle.read(min(1024 * 1024, allowance)):
                        digest.update(chunk)
                        allowance -= len(chunk)
                        remaining -= len(chunk)
                else:
                    first = allowance // 2
                    last = allowance - first
                    digest.update(handle.read(first))
                    handle.seek(max(0, metadata.st_size - last))
                    digest.update(handle.read(last))
                    remaining -= allowance
                    digest.update(b"\0sampled")
        except OSError as exc:
            digest.update(f"\0unreadable:{type(exc).__name__}".encode())
    return digest.hexdigest()


def _run(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)


def git_output(root: Path, *args: str) -> str:
    """Run one read-only Git command and return stripped stdout."""
    result = _run(root, ["git", *args])
    if result.returncode != 0:
        raise GitStateError(
            result.stderr.strip() or f"Git command failed: {' '.join(args)}"
        )
    return result.stdout.strip()


def repository_root(path: Path) -> Path:
    """Resolve the containing Git worktree root."""
    return Path(git_output(path, "rev-parse", "--show-toplevel")).resolve()


def repository_remote(path: Path) -> str | None:
    """Return the configured origin URL when one exists."""
    result = _run(path, ["git", "remote", "get-url", "origin"])
    return (
        result.stdout.strip()
        if result.returncode == 0 and result.stdout.strip()
        else None
    )


def snapshot(root: Path) -> GitSnapshot:
    """Return a deterministic Git snapshot including untracked paths."""
    head = git_output(root, "rev-parse", "HEAD")
    tree = git_output(root, "rev-parse", "HEAD^{tree}")
    status_result = subprocess.run(
        ["git", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if status_result.returncode != 0:
        raise GitStateError(status_result.stderr.decode(errors="replace").strip())
    raw = status_result.stdout
    modified: set[str] = set()
    deleted: set[str] = set()
    renamed: set[str] = set()
    untracked: set[str] = set()
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        text = record.decode("utf-8", errors="surrogateescape")
        prefix = text[:2]
        if prefix == "? ":
            path = normalize_repository_path(text[2:])
            if path is not None:
                untracked.add(path)
            continue
        if prefix == "! ":
            continue
        record_type = text[:1]
        if record_type == "1":
            fields = text.split(" ", 8)
            if len(fields) != 9:
                continue
            xy = fields[1]
            path = normalize_repository_path(fields[8])
        elif record_type == "2":
            fields = text.split(" ", 9)
            if len(fields) != 10:
                continue
            xy = fields[1]
            path = normalize_repository_path(fields[9])
            if path is not None:
                renamed.add(path)
            if index < len(records):
                original_raw = records[index].decode("utf-8", errors="surrogateescape")
                index += 1
                original = normalize_repository_path(original_raw)
                if original is not None:
                    deleted.add(original)
        elif record_type == "u":
            fields = text.split(" ", 10)
            if len(fields) != 11:
                continue
            xy = fields[1]
            path = normalize_repository_path(fields[10])
        else:
            continue
        if path is None:
            continue
        if "D" in xy:
            deleted.add(path)
        else:
            modified.add(path)
    canonical_status = b"\0".join(
        f"{kind} {path}".encode("utf-8", errors="surrogateescape")
        for kind, paths in (
            ("M", modified),
            ("D", deleted),
            ("R", renamed),
            ("?", untracked),
        )
        for path in sorted(paths)
    )
    return GitSnapshot(
        head=head,
        tree=tree,
        dirty_hash=_dirty_state_hash(
            root,
            canonical_status,
            paths=modified | deleted | renamed | untracked,
            deleted=deleted,
        ),
        modified=frozenset(modified),
        deleted=frozenset(deleted),
        renamed=frozenset(renamed),
        untracked=frozenset(untracked),
    )
