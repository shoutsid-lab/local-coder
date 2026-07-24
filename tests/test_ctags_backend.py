from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runtime.search.ctags_backend import CtagsBackend, CtagsError
from runtime.search.profile import load_search_profile
from runtime.search.registry import RepositoryRecord

ROOT = Path(__file__).resolve().parents[1]


def test_ctags_build_and_exact_symbol_lookup(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "def calculate_total(values):\n    return sum(values)\n",
        encoding="utf-8",
    )
    index = tmp_path / "symbols.jsonl"
    record = RepositoryRecord(
        id="fixture",
        path=str(tmp_path),
        ctags_index=str(index),
    )
    tag = {
        "_type": "tag",
        "name": "calculate_total",
        "path": "sample.py",
        "line": 1,
        "language": "Python",
        "kind": "function",
        "pattern": "/^def calculate_total(values):$/",
    }

    monkeypatch.setattr(
        "runtime.search.ctags_backend.shutil.which",
        lambda _: "/usr/bin/ctags",
    )
    monkeypatch.setattr(
        CtagsBackend,
        "_tracked_files",
        staticmethod(lambda _: ("sample.py",)),
    )
    monkeypatch.setattr(
        "runtime.search.ctags_backend.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(tag) + "\n", ""
        ),
    )
    backend = CtagsBackend(load_search_profile(ROOT))
    backend.build(record)
    hits = backend.search(record, "calculate_total")
    assert hits[0].backend == "ctags"
    assert hits[0].match_kind == "symbol"
    assert hits[0].path == "sample.py"
    assert hits[0].start_line == 1
    assert hits[0].reason.startswith("persistent Ctags")


def test_ctags_generates_current_dirty_path_symbols(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "dirty.py"
    source.write_text("def dirty_symbol():\n    return 1\n", encoding="utf-8")
    tag = {
        "_type": "tag",
        "name": "dirty_symbol",
        "path": "dirty.py",
        "line": 1,
        "language": "Python",
        "kind": "function",
    }

    monkeypatch.setattr(
        "runtime.search.ctags_backend.shutil.which",
        lambda _: "/usr/bin/ctags",
    )
    monkeypatch.setattr(
        "runtime.search.ctags_backend.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(tag) + "\n", ""
        ),
    )
    backend = CtagsBackend(load_search_profile(ROOT))

    hits = backend.search_current_paths(
        tmp_path,
        ("dirty.py",),
        "dirty_symbol",
        repository_id="fixture",
    )

    assert hits[0].path == "dirty.py"
    assert hits[0].reason.startswith("current-worktree Ctags")


def test_ctags_skips_files_above_committed_size_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.py"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    backend = CtagsBackend(load_search_profile(ROOT))
    monkeypatch.setattr(
        "runtime.search.ctags_backend.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ctags should not run for oversized input")
        ),
    )

    entries = backend._generate(
        tmp_path,
        ("large.py",),
        timeout_seconds=1.0,
    )

    assert entries == []


def test_ctags_rejects_oversized_cache_before_reading(tmp_path: Path) -> None:
    index = tmp_path / "symbols.jsonl"
    with index.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    record = RepositoryRecord(
        id="fixture",
        path=str(tmp_path),
        ctags_index=str(index),
    )
    backend = CtagsBackend(load_search_profile(ROOT))

    with pytest.raises(CtagsError, match="bounded cache limit"):
        backend.search(record, "symbol")
