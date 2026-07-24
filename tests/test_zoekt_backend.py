from __future__ import annotations

import json
import subprocess
from pathlib import Path

from runtime.search.contracts import RepositorySearchRequest
from runtime.search.profile import load_search_profile
from runtime.search.registry import RepositoryRecord
from runtime.search.zoekt_backend import ZoektBackend

ROOT = Path(__file__).resolve().parents[1]


def test_zoekt_uses_jsonl_and_parses_machine_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    index = tmp_path / "index"
    index.mkdir()
    record = RepositoryRecord(
        id="fixture",
        path=str(tmp_path),
        zoekt_index=str(index),
    )
    payload = {
        "FileName": "runtime/search/engine.py",
        "Repository": "fixture",
        "Score": 91.5,
        "LineMatches": [{"LineNumber": 12, "Line": "class RepositorySearchEngine:\n"}],
    }
    observed: list[str] = []

    def fake_which(binary: str) -> str:
        return f"/usr/bin/{binary}"

    def fake_run(args, **kwargs):
        observed.extend(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload) + "\n", "")

    monkeypatch.setattr("runtime.search.zoekt_backend.shutil.which", fake_which)
    monkeypatch.setattr("runtime.search.zoekt_backend.subprocess.run", fake_run)
    backend = ZoektBackend(load_search_profile(ROOT))
    hits = backend.search(
        RepositorySearchRequest(
            query="RepositorySearchEngine",
            repository_ids=("fixture",),
            worktree=tmp_path,
            mode="symbol",
        ),
        record=record,
    )
    assert "-jsonl" in observed
    assert "-sym" in observed
    assert hits[0].path == "runtime/search/engine.py"
    assert hits[0].start_line == 12
    assert hits[0].score == 91.5


def test_zoekt_validate_runs_a_bounded_probe(monkeypatch, tmp_path: Path) -> None:
    index = tmp_path / "index"
    index.mkdir()
    record = RepositoryRecord(
        id="fixture",
        path=str(tmp_path),
        zoekt_index=str(index),
    )
    observed: list[str] = []

    monkeypatch.setattr(
        "runtime.search.zoekt_backend.shutil.which",
        lambda binary: f"/usr/bin/{binary}",
    )

    def fake_run(args, **kwargs):
        observed.extend(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("runtime.search.zoekt_backend.subprocess.run", fake_run)
    backend = ZoektBackend(load_search_profile(ROOT))

    assert backend.validate(record)
    assert "-index_dir" in observed
    assert "-jsonl" in observed
