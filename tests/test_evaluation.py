from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation.outcomes import FailureClass, normalize_run
from evaluation.contract_worker import run_scenario
from evaluation.manifests import ManifestError, load_holdout_oracle, load_suite
from evaluation.miner import campaign_candidate_limit, mine_improvement_brief
from evaluation.supervisor import (
    EvaluationBudget,
    EvaluationError,
    Supervisor,
    evaluate_pair,
)
from runtime.migrations import MigrationError
from runtime.state import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]


def test_state_migration_preserves_legacy_runs(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                mode TEXT NOT NULL, repository TEXT NOT NULL, base_branch TEXT,
                branch TEXT, worktree TEXT, result TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """)
        connection.execute("""
            INSERT INTO runs VALUES (
                'legacy', 'task', 'created', 'agentic', '/repo', 'main',
                NULL, NULL, NULL, NULL, '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            )
            """)

    store = StateStore(database)

    assert store.schema_version() == SCHEMA_VERSION
    assert store.run_details("legacy")["task"] == "task"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_read_only_store_does_not_initialize_a_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        StateStore(database, read_only=True)

    assert not database.exists()


def test_migration_rejects_a_noncontiguous_ledger(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")

    with pytest.raises(MigrationError, match="not contiguous"):
        StateStore(database)


def test_migration_rejects_a_future_schema(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(MigrationError, match="newer than supported"):
        StateStore(database)


def test_failed_legacy_migration_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "malformed.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO runs VALUES ('legacy')")

    with pytest.raises(MigrationError, match="incompatible columns"):
        StateStore(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "schema_migrations" not in tables
        assert connection.execute("SELECT id FROM runs").fetchone()[0] == "legacy"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_read_only_store_reports_unmigrated_schema_without_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")

    assert StateStore(database, read_only=True).schema_version() == 0
    with sqlite3.connect(database) as connection:
        assert "schema_migrations" not in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_run_details_returns_steps_artifacts_and_metrics(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Inspect evidence",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    step_id = store.start_step(run_id, agent_role="explorer", summary="inspect")
    store.complete_step(step_id, status="completed")
    store.add_artifact(run_id, kind="diff", content="diff --git a/a.py b/a.py")
    store.add_model_metrics(
        run_id,
        route="local-plan",
        prompt_tokens=10,
        completion_tokens=4,
        duration_ms=2,
    )

    details = store.run_details(run_id)

    assert details is not None
    assert details["steps"][0]["status"] == "completed"
    assert details["artifacts"][0]["kind"] == "diff"
    assert details["model_metrics"][0]["prompt_tokens"] == 10


def test_historical_outcome_is_deterministic_and_unknown_is_not_zero() -> None:
    details = {
        "id": "run-1",
        "task": "Edit a.py",
        "status": "needs_attention",
        "repository": "/repo",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "agents": [
            {
                "skill": "atomic-implementation",
                "model_route": "local-fast",
            }
        ],
        "steps": [],
        "tool_calls": [
            {
                "tool_name": "apply_atomic_edit",
                "status": "error",
            }
        ],
        "artifacts": [
            {
                "kind": "diff",
                "content": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py",
            },
            {
                "kind": "review",
                "content": json.dumps({"verdict": "needs_attention"}),
            },
        ],
        "verification": [],
        "model_metrics": [],
        "result": "Rejected editor attempts: 1",
    }

    first = normalize_run(details)
    second = normalize_run(details)

    assert first == second
    assert first.prompt_tokens is None
    assert first.completion_tokens is None
    assert first.model_calls is None
    assert first.actual_changed_paths == ("a.py",)
    assert FailureClass.EDITOR in first.failures
    assert FailureClass.REVIEW in first.failures


def test_trusted_contract_suites_match_their_oracles() -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    development = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    holdout = load_suite(
        ROOT / "evaluation" / "holdout" / "atomic-holdout-v1.json",
        expected_visibility="holdout",
    )
    holdout_oracle, _ = load_holdout_oracle(
        ROOT / "evaluation" / "oracles" / "atomic-holdout-v1.json",
        holdout,
    )

    for case in development.cases:
        assert run_scenario(ROOT, case.scenario) == case.oracle
    for case in holdout.cases:
        assert run_scenario(ROOT, case.scenario) == holdout_oracle[case.case_id]


def test_holdout_oracle_mismatch_fails_closed(tmp_path: Path) -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    manifest = load_suite(
        ROOT / "evaluation" / "holdout" / "atomic-holdout-v1.json",
        expected_visibility="holdout",
    )
    oracle = tmp_path / "oracle.json"
    oracle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": manifest.suite_id,
                "cases": {"wrong-case": {}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="case IDs differ"):
        load_holdout_oracle(oracle, manifest)


def test_failure_miner_emits_one_deterministic_bounded_brief() -> None:
    details = {
        "id": "run-1",
        "task": "task",
        "status": "failed_verification",
        "repository": "/repo",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "agents": [],
        "tool_calls": [],
        "artifacts": [],
        "verification": [{"passed": 0}],
        "model_metrics": [],
    }
    outcome = normalize_run(details)

    first = mine_improvement_brief(
        [outcome],
        baseline_commit="abc",
        allowed_files=["runtime/editor.py"],
        forbidden_files=["evaluation/"],
        acceptance_metrics=[{"case_id": "exact-edit", "direction": "increase"}],
        suite_hash="suite",
        budget={"processes": 1},
        rollback_condition="Any safety regression",
    )
    second = mine_improvement_brief(
        [outcome],
        baseline_commit="abc",
        allowed_files=["runtime/editor.py"],
        forbidden_files=["evaluation/"],
        acceptance_metrics=[{"case_id": "exact-edit", "direction": "increase"}],
        suite_hash="suite",
        budget={"processes": 1},
        rollback_condition="Any safety regression",
    )

    assert first == second
    assert first.failure_class == "verification"
    assert first.evidence_run_ids == ("run-1",)


def test_campaign_starts_with_one_candidate_until_ten_clean_runs(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "agent.db")

    assert campaign_candidate_limit(store) == 1
    for _ in range(10):
        campaign_id = store.create_campaign(
            baseline_commit="base",
            suite_hash="suite",
            budget={"processes": 1},
            max_candidates=1,
        )
        store.update_campaign_status(campaign_id, "completed_clean")

    assert campaign_candidate_limit(store) == 3


def test_campaign_requires_one_human_approved_brief(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"processes": 1},
        max_candidates=1,
    )
    brief = {
        "id": "brief",
        "evidence_run_ids": ["run"],
        "baseline_commit": "base",
        "failure_class": "verification",
        "hypothesis": "hypothesis",
        "allowed_files": ["runtime/editor.py"],
        "forbidden_files": ["evaluation/"],
        "acceptance_metrics": [{"case_id": "case"}],
        "suite_hash": "suite",
        "budget": {"processes": 1},
        "rollback_condition": "regression",
    }
    store.add_improvement_brief(campaign_id, brief)

    with pytest.raises(ValueError, match="human-approved brief"):
        store.create_evaluation(
            campaign_id=campaign_id,
            baseline_commit="base",
            candidate_commit="candidate",
            suite_id="suite",
            suite_hash="suite",
            holdout_hash="holdout",
            environment_hash="environment",
            repetitions=1,
            budget={"processes": 1},
        )

    store.approve_improvement_brief(
        "brief",
        actor="human",
        rationale="The bounded hypothesis is safe to evaluate.",
    )
    run_id = store.create_run(
        task="Build candidate",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id="brief",
        overlay_hash=None,
        overlay=None,
    )
    store.complete_candidate_build(
        build_id,
        run_id=run_id,
        status="awaiting_approval",
        branch="candidate",
        worktree=str(tmp_path / "candidate"),
    )
    evaluation_id = store.create_evaluation(
        campaign_id=campaign_id,
        baseline_commit="base",
        candidate_commit="candidate",
        suite_id="suite",
        suite_hash="suite",
        holdout_hash="holdout",
        environment_hash="environment",
        repetitions=1,
        budget={"processes": 1},
        build_id=build_id,
    )

    assert evaluation_id
    with pytest.raises(ValueError, match="candidate limit"):
        store.create_evaluation(
            campaign_id=campaign_id,
            baseline_commit="base",
            candidate_commit="candidate-2",
            suite_id="suite",
            suite_hash="suite",
            holdout_hash="holdout",
            environment_hash="environment",
            repetitions=1,
            budget={"processes": 1},
            build_id=build_id,
        )

    assert store.campaign_details(campaign_id)["evaluations"][0]["build_id"] == build_id


def test_supervisor_records_timeout_without_retrying(tmp_path: Path) -> None:
    supervisor = object.__new__(Supervisor)
    supervisor.trusted_root = tmp_path
    supervisor.budget = EvaluationBudget(
        campaign_wall_seconds=5,
        process_wall_seconds=1,
        max_processes=2,
    )
    supervisor.bwrap = "bwrap"
    supervisor.process_count = 0
    supervisor.started = time.monotonic()

    result = supervisor._run(["/usr/bin/python3", "-c", "import time; time.sleep(5)"])

    assert result.timed_out is True
    assert result.returncode is None
    assert supervisor.process_count == 1


def _git_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "evaluation@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Evaluation"], cwd=path, check=True)
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=path, check=True)


def test_environment_hash_mismatch_fails_before_candidate_execution(
    tmp_path: Path,
) -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _git_repository(baseline)
    _git_repository(candidate)
    development = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    holdout = load_suite(
        ROOT / "evaluation" / "holdout" / "atomic-holdout-v1.json",
        expected_visibility="holdout",
    )
    oracle, oracle_hash = load_holdout_oracle(
        ROOT / "evaluation" / "oracles" / "atomic-holdout-v1.json",
        holdout,
    )

    with (
        patch("evaluation.supervisor.Supervisor") as supervisor,
        pytest.raises(EvaluationError, match="environment hash mismatch"),
    ):
        evaluate_pair(
            trusted_root=ROOT,
            baseline=baseline,
            candidate=candidate,
            development=development,
            holdout=holdout,
            holdout_oracle=oracle,
            holdout_oracle_hash=oracle_hash,
            repetitions=1,
            budget=EvaluationBudget(),
            allowed_candidate_paths={"tracked.txt"},
            expected_environment_hash="wrong",
        )

    supervisor.assert_not_called()
