from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evaluation.outcomes import stable_hash
from runtime.orchestrator import authoritative_manager_result
from runtime.tools import Worktree
from runtime.gepa_experiment import (
    PLANNER_SEED_CASES,
    PLANNER_SEED_FILE,
    collect_planner_seed_corpus,
    planner_seed_suite,
    validate_planner_seed_suite,
)


def _git_repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)


def _planner_records() -> list[dict[str, object]]:
    records = []
    for index, case in enumerate(PLANNER_SEED_CASES, start=1):
        records.append(
            {
                "role": "planner",
                "split": case.expected_split,
                "task": case.task,
                "outcome": {"score": 1.0},
                "example_id": f"example-{index}",
            }
        )
    return records


def test_planner_seed_suite_has_exact_ready_split(tmp_path: Path) -> None:
    profile = tmp_path / PLANNER_SEED_FILE
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "".join(f"{case.old_text}\n" for case in PLANNER_SEED_CASES),
        encoding="utf-8",
    )

    suite = validate_planner_seed_suite(tmp_path)

    assert suite == planner_seed_suite()
    assert [case["actual_split"] for case in suite["cases"]].count("train") == 3
    assert [case["actual_split"] for case in suite["cases"]].count("dev") == 2
    assert [case["actual_split"] for case in suite["cases"]].count("holdout") == 1


def test_collection_uses_real_isolated_diffs_and_exports_ready_dataset(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    profile = repository / PLANNER_SEED_FILE
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "".join(f"{case.old_text}\n" for case in PLANNER_SEED_CASES),
        encoding="utf-8",
    )
    _git_repository(repository)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    summaries = []
    for index, case in enumerate(PLANNER_SEED_CASES, start=1):
        worktree = worktrees / str(index)
        worktree.mkdir()
        target = worktree / PLANNER_SEED_FILE
        target.parent.mkdir(parents=True)
        target.write_text(profile.read_text(encoding="utf-8"), encoding="utf-8")
        _git_repository(worktree)
        target.write_text(
            target.read_text(encoding="utf-8").replace(case.old_text, case.new_text),
            encoding="utf-8",
        )
        summaries.append(
            SimpleNamespace(
                run_id=f"run-{index}",
                status="awaiting_approval",
                branch=f"agent/planner-{index}",
                worktree=str(worktree),
                verification_passed=True,
                review_verdict="pass",
            )
        )

    class FakeOrchestrator:
        def __init__(self, _config: object) -> None:
            self.summary = summaries.pop(0)

        def run(self, _task: str) -> object:
            return self.summary

    database = repository / ".local-coder" / "state" / "agent.db"
    database.parent.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    report = tmp_path / "collection"
    dataset_manifest = {"dataset_hash": "dataset-hash"}
    records = _planner_records()

    def fake_exporter(
        source_database: Path,
        output: Path,
        *,
        run_ids: list[str],
        limit: int,
    ) -> dict[str, object]:
        assert source_database == database.resolve()
        assert run_ids == [f"run-{index}" for index in range(1, 7)]
        assert limit == 6
        output.mkdir()
        return dataset_manifest

    with patch(
        "runtime.gepa_experiment.load_gepa_dataset",
        return_value=(dataset_manifest, records),
    ):
        result = collect_planner_seed_corpus(
            repository,
            database,
            dataset,
            report,
            orchestrator_factory=FakeOrchestrator,
            exporter=fake_exporter,
        )

    assert result["report"]["readiness"]["ready"] is True
    assert result["report"]["readiness"]["counts"]["train"] == 3
    assert result["report"]["readiness"]["counts"]["dev"] == 2
    assert result["report"]["readiness"]["counts"]["holdout"] == 1
    manifest = json.loads((report / "manifest.json").read_text(encoding="utf-8"))
    hash_input = dict(manifest)
    claimed_hash = hash_input.pop("manifest_hash")
    assert claimed_hash == stable_hash(hash_input)


def test_authoritative_manager_result_uses_worktree_branch(tmp_path: Path) -> None:
    repository = tmp_path / "worktree"
    repository.mkdir()
    (repository / "canary.txt").write_text("before\n", encoding="utf-8")
    _git_repository(repository)
    (repository / "canary.txt").write_text("after\n", encoding="utf-8")

    rendered = authoritative_manager_result(
        Worktree(repository, "agent/authoritative", "main")
    )
    result = json.loads(rendered)

    assert result["branch"] == "agent/authoritative"
    assert result["changed_files"] == ["canary.txt"]
    assert result["manager_report"] == "preserved as manager_result artifact"


def test_collection_rejects_non_repository_database(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    profile = repository / PLANNER_SEED_FILE
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "".join(f"{case.old_text}\n" for case in PLANNER_SEED_CASES),
        encoding="utf-8",
    )
    _git_repository(repository)

    try:
        collect_planner_seed_corpus(
            repository,
            tmp_path / "other.db",
            tmp_path / "dataset",
            tmp_path / "report",
        )
    except ValueError as exc:
        assert "repository audit database" in str(exc)
    else:
        raise AssertionError("collection accepted a non-repository database")
