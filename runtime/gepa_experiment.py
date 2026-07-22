"""Reproducible real-run corpus collection for the first planner GEPA experiment."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from evaluation.outcomes import stable_hash
from runtime.dspy_programs.gepa_dataset import (
    export_gepa_dataset,
    load_gepa_dataset,
    split_for_task,
)
from runtime.dspy_programs.gepa_runner import assess_dataset_readiness
from runtime.orchestrator import AgentOrchestrator, OrchestratorConfig, RunSummary
from runtime.tools import Worktree, command, remove_worktree, require_clean_repository

PLANNER_SEED_SCHEMA_VERSION = 1
PLANNER_SEED_FILE = "profiles/gepa-planner-seed.txt"


class GepaExperimentError(ValueError):
    """Raised when a corpus collection cannot proceed safely."""


@dataclass(frozen=True)
class PlannerSeedCase:
    """One deterministic isolated edit used to collect audited planner evidence."""

    case_id: str
    old_text: str
    new_text: str
    identity: str
    expected_split: str

    @property
    def task(self) -> str:
        return (
            f"In {PLANNER_SEED_FILE}, replace exactly one occurrence of "
            f'"{self.old_text}" with "{self.new_text}". '
            "Change no other text or file.\n\n"
            f"GEPA corpus case: {self.identity}."
        )


PLANNER_SEED_CASES = (
    PlannerSeedCase(
        "planner-1",
        "GEPA_PLANNER_CASE_1=before",
        "GEPA_PLANNER_CASE_1=after",
        "planner-1-1",
        "train",
    ),
    PlannerSeedCase(
        "planner-2",
        "GEPA_PLANNER_CASE_2=before",
        "GEPA_PLANNER_CASE_2=after",
        "planner-2-0",
        "train",
    ),
    PlannerSeedCase(
        "planner-3",
        "GEPA_PLANNER_CASE_3=before",
        "GEPA_PLANNER_CASE_3=after",
        "planner-3-0",
        "train",
    ),
    PlannerSeedCase(
        "planner-4",
        "GEPA_PLANNER_CASE_4=before",
        "GEPA_PLANNER_CASE_4=after",
        "planner-4-14",
        "dev",
    ),
    PlannerSeedCase(
        "planner-5",
        "GEPA_PLANNER_CASE_5=before",
        "GEPA_PLANNER_CASE_5=after",
        "planner-5-0",
        "dev",
    ),
    PlannerSeedCase(
        "planner-6",
        "GEPA_PLANNER_CASE_6=before",
        "GEPA_PLANNER_CASE_6=after",
        "planner-6-5",
        "holdout",
    ),
)


def planner_seed_suite() -> dict[str, Any]:
    """Return the immutable suite identity and expected split allocation."""
    cases = [
        {
            **asdict(case),
            "task": case.task,
            "task_hash": hashlib.sha256(case.task.encode("utf-8")).hexdigest(),
            "actual_split": split_for_task(case.task),
        }
        for case in PLANNER_SEED_CASES
    ]
    payload = {
        "schema_version": PLANNER_SEED_SCHEMA_VERSION,
        "target_role": "planner",
        "expected_file": PLANNER_SEED_FILE,
        "cases": cases,
    }
    payload["suite_hash"] = stable_hash(payload)
    return payload


def validate_planner_seed_suite(repository: Path) -> dict[str, Any]:
    """Validate task splits and source sentinels before any model call."""
    repository = repository.resolve()
    path = repository / PLANNER_SEED_FILE
    if not path.is_file():
        raise GepaExperimentError(f"Planner seed file is missing: {path}")
    content = path.read_text(encoding="utf-8")
    suite = planner_seed_suite()
    seen_tasks: set[str] = set()
    for case in PLANNER_SEED_CASES:
        if case.task in seen_tasks:
            raise GepaExperimentError("Planner seed suite contains a duplicate task.")
        seen_tasks.add(case.task)
        actual_split = split_for_task(case.task)
        if actual_split != case.expected_split:
            raise GepaExperimentError(
                f"Planner seed split drift for {case.case_id}: "
                f"expected {case.expected_split}, got {actual_split}."
            )
        if content.count(case.old_text) != 1:
            raise GepaExperimentError(
                f"Planner seed source must contain {case.old_text!r} exactly once."
            )
        if case.new_text in content:
            raise GepaExperimentError(
                f"Planner seed source already contains {case.new_text!r}."
            )
    return suite


def _changed_files(worktree: Path) -> list[str]:
    result = command(
        ["git", "diff", "--name-only"],
        cwd=worktree,
        check=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def _validate_summary(case: PlannerSeedCase, summary: RunSummary) -> dict[str, Any]:
    if summary.status != "awaiting_approval":
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} ended with status {summary.status}."
        )
    if not summary.verification_passed or summary.review_verdict != "pass":
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} did not pass verification and review."
        )
    if not summary.branch or not summary.branch.startswith("agent/"):
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} has no authoritative agent branch."
        )
    if not summary.worktree:
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} has no worktree path."
        )
    worktree = Path(summary.worktree)
    changed_files = _changed_files(worktree)
    if changed_files != [PLANNER_SEED_FILE]:
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} changed unexpected files: "
            f"{changed_files}."
        )
    edited = (worktree / PLANNER_SEED_FILE).read_text(encoding="utf-8")
    if case.new_text not in edited or case.old_text in edited:
        raise GepaExperimentError(
            f"Planner seed case {case.case_id} did not apply the exact sentinel edit."
        )
    return {
        "case_id": case.case_id,
        "expected_split": case.expected_split,
        "task_hash": hashlib.sha256(case.task.encode("utf-8")).hexdigest(),
        "run_id": summary.run_id,
        "status": summary.status,
        "branch": summary.branch,
        "worktree": summary.worktree,
        "verification_passed": summary.verification_passed,
        "review_verdict": summary.review_verdict,
        "changed_files": changed_files,
    }


def _write_collection_report(output: Path, report: dict[str, Any]) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise GepaExperimentError(
            f"GEPA collection report directory already exists: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "collection"
        staging.mkdir()
        report_path = staging / "report.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": PLANNER_SEED_SCHEMA_VERSION,
            "suite_hash": report["suite_hash"],
            "dataset_hash": report["dataset_hash"],
            "report_hash": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
        manifest["manifest_hash"] = stable_hash(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
    return manifest


def collect_planner_seed_corpus(
    repository: Path,
    database: Path,
    dataset_output: Path,
    report_output: Path,
    *,
    max_steps: int = 12,
    cleanup_successful_worktrees: bool = False,
    orchestrator_factory: Callable[[OrchestratorConfig], AgentOrchestrator] = (
        AgentOrchestrator
    ),
    exporter: Callable[..., dict[str, Any]] = export_gepa_dataset,
) -> dict[str, Any]:
    """Collect six real audited runs and export a planner-ready dataset."""
    if max_steps <= 0:
        raise GepaExperimentError("max_steps must be positive.")
    repository = repository.resolve()
    database = database.resolve()
    dataset_output = dataset_output.resolve()
    report_output = report_output.resolve()
    expected_database = (repository / ".local-coder" / "state" / "agent.db").resolve()
    if database != expected_database:
        raise GepaExperimentError(
            "Planner seed collection must use the repository audit database: "
            f"{expected_database}"
        )
    require_clean_repository(repository)
    suite = validate_planner_seed_suite(repository)
    if dataset_output.exists():
        raise GepaExperimentError(
            f"GEPA planner dataset directory already exists: {dataset_output}"
        )
    if report_output.exists():
        raise GepaExperimentError(
            f"GEPA collection report directory already exists: {report_output}"
        )
    if (
        dataset_output == report_output
        or dataset_output in report_output.parents
        or report_output in dataset_output.parents
    ):
        raise GepaExperimentError(
            "GEPA dataset and collection report directories must be separate."
        )

    collected: list[dict[str, Any]] = []
    for case in PLANNER_SEED_CASES:
        config = OrchestratorConfig(
            repository=repository,
            max_steps=max_steps,
            keep_worktree=True,
            mode="agentic",
            expected_changed_paths=(PLANNER_SEED_FILE,),
        )
        summary = orchestrator_factory(config).run(case.task)
        record = _validate_summary(case, summary)
        collected.append(record)
        if cleanup_successful_worktrees:
            remove_worktree(
                repository,
                Worktree(
                    path=Path(summary.worktree),
                    branch=str(summary.branch),
                    base_branch="main",
                ),
                force=True,
            )

    run_ids = [record["run_id"] for record in collected]
    dataset_manifest = exporter(
        database,
        dataset_output,
        run_ids=run_ids,
        limit=len(run_ids),
    )
    loaded_manifest, records = load_gepa_dataset(dataset_output)
    if loaded_manifest["dataset_hash"] != dataset_manifest["dataset_hash"]:
        raise GepaExperimentError("Exported dataset hash changed during verification.")
    readiness = assess_dataset_readiness(records, role="planner")
    expected_counts = {
        "total": 6,
        "train": 3,
        "dev": 2,
        "holdout": 1,
        "distinct_tasks": 6,
        "successful": 6,
        "imperfect": 0,
    }
    if not readiness["ready"] or readiness["counts"] != expected_counts:
        raise GepaExperimentError(
            "Planner seed export did not produce the expected ready split: "
            f"{readiness}."
        )

    report = {
        "schema_version": PLANNER_SEED_SCHEMA_VERSION,
        "suite_hash": suite["suite_hash"],
        "dataset": str(dataset_output),
        "dataset_hash": dataset_manifest["dataset_hash"],
        "run_ids": run_ids,
        "cases": collected,
        "readiness": readiness,
        "cleanup_successful_worktrees": cleanup_successful_worktrees,
        "optimization": "not_performed",
        "activation": "not_performed",
        "promotion": "not_performed",
    }
    collection_manifest = _write_collection_report(report_output, report)
    return {
        "suite": suite,
        "dataset_manifest": dataset_manifest,
        "collection_manifest": collection_manifest,
        "report": report,
    }
