from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation.outcomes import (
    FailureClass,
    candidate_trajectory_evidence,
    hash_text,
    normalize_run,
    stable_hash,
)
from evaluation.audit import audit_campaign
from evaluation.scorecard import build_scorecard
from evaluation.contract_worker import run_scenario
from evaluation.manifests import ManifestError, load_holdout_oracle, load_suite
from evaluation.miner import campaign_candidate_limit, mine_improvement_brief
from evaluation.supervisor import (
    EvaluationBudget,
    CaseResult,
    EvaluationError,
    PairedEvaluation,
    ProcessResult,
    Supervisor,
    environment_identity,
    evaluate_pair,
)
from runtime.migrations import MIGRATIONS, MigrationError
from runtime.state import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]


def _test_holdout(tmp_path: Path) -> tuple[Path, Path]:
    """Write a non-secret unit fixture; production rotations are never tracked."""
    manifest = tmp_path / "holdout.json"
    oracle = tmp_path / "holdout-oracle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "unit-holdout-v1",
                "visibility": "holdout",
                "cases": [
                    {"id": "unit-sequential", "scenario": "sequential_edits"},
                    {"id": "unit-protected", "scenario": "protected_alias"},
                    {"id": "unit-empty", "scenario": "empty_untracked_diff"},
                ],
            }
        ),
        encoding="utf-8",
    )
    oracle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "unit-holdout-v1",
                "cases": {
                    "unit-sequential": {
                        "changed": ["sample.txt"],
                        "content": "A B\n",
                    },
                    "unit-protected": {
                        "error": "EditorError",
                        "content": "before\n",
                    },
                    "unit-empty": {"has_empty": True},
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest, oracle


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


def test_state_migration_preserves_v7_campaign_identity_gap(tmp_path: Path) -> None:
    database = tmp_path / "v7.db"
    with sqlite3.connect(database) as connection:
        for migration in MIGRATIONS[:7]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (migration.version, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.execute(
            """
            INSERT INTO evaluation_campaigns VALUES (
                'campaign', 'base', 'active', 'suite', '{"processes":1}',
                1, '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )

    store = StateStore(database)
    details = store.campaign_details("campaign")

    assert store.schema_version() == SCHEMA_VERSION
    assert details is not None
    assert details["baseline_commit"] == "base"
    assert details["holdout_hash"] is None
    assert details["environment_hash"] is None


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


def test_trusted_contract_suites_match_their_oracles(tmp_path: Path) -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    development = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    manifest_path, oracle_path = _test_holdout(tmp_path)
    holdout = load_suite(manifest_path, expected_visibility="holdout")
    holdout_oracle, _ = load_holdout_oracle(oracle_path, holdout)

    for case in development.cases:
        assert run_scenario(ROOT, case.scenario) == case.oracle
    for case in holdout.cases:
        assert run_scenario(ROOT, case.scenario) == holdout_oracle[case.case_id]


def test_holdout_oracle_mismatch_fails_closed(tmp_path: Path) -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    manifest_path, _ = _test_holdout(tmp_path)
    manifest = load_suite(manifest_path, expected_visibility="holdout")
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
        holdout_hash="holdout",
        environment_hash="environment",
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


def test_campaign_evaluation_requires_frozen_holdout_and_environment(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "agent.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"processes": 1},
        max_candidates=1,
        holdout_hash="holdout-a",
        environment_hash="environment-a",
    )
    brief = {
        "id": "brief-identity",
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
    store.approve_improvement_brief(
        brief["id"],
        actor="human",
        rationale="Freeze all trusted evaluation identities.",
    )
    run_id = store.create_run(
        task="Build candidate",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id=brief["id"],
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

    common = {
        "campaign_id": campaign_id,
        "build_id": build_id,
        "baseline_commit": "base",
        "candidate_commit": "candidate",
        "suite_id": "suite",
        "suite_hash": "suite",
        "repetitions": 1,
        "budget": {"processes": 1},
    }
    with pytest.raises(ValueError, match="holdout differs"):
        store.create_evaluation(
            **common,
            holdout_hash="holdout-b",
            environment_hash="environment-a",
        )
    with pytest.raises(ValueError, match="environment differs"):
        store.create_evaluation(
            **common,
            holdout_hash="holdout-a",
            environment_hash="environment-b",
        )

    assert store.campaign_details(campaign_id)["evaluations"] == []


def test_complete_recursive_campaign_control_cycle(tmp_path: Path) -> None:
    """Demonstrate lineage through human decision without performing a Git action."""
    store = StateStore(tmp_path / "campaign.db")
    budget = {
        "campaign_wall_seconds": 30,
        "process_wall_seconds": 5,
        "max_processes": 8,
        "max_output_bytes": 10_000,
        "max_memory_mb": 256,
        "max_file_mb": 4,
        "max_disk_mb": 32,
        "max_prompt_tokens": 100,
        "max_completion_tokens": 50,
        "max_model_calls": 4,
    }
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget=budget,
        max_candidates=1,
        holdout_hash="secret-hash",
        environment_hash="environment",
    )
    brief = {
        "id": "brief-cycle",
        "evidence_run_ids": ["historical-run"],
        "baseline_commit": "base",
        "failure_class": "verification",
        "hypothesis": "One bounded editor change improves the target.",
        "allowed_files": ["runtime/editor.py"],
        "forbidden_files": ["evaluation/"],
        "acceptance_metrics": [{"case_id": "target"}],
        "suite_hash": "suite",
        "budget": budget,
        "rollback_condition": "Any safety regression",
    }
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief["id"], actor="human", rationale="Bounded and falsifiable."
    )
    run_id = store.create_run(
        task="Implement approved brief",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    store.add_artifact(
        run_id,
        kind="diff",
        content="diff --git a/runtime/editor.py b/runtime/editor.py",
    )
    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="pass",
        duration_ms=1,
    )
    store.add_artifact(
        run_id,
        kind="review",
        content=json.dumps({"verdict": "pass"}),
    )
    store.add_model_metrics(
        run_id,
        route="local-plan",
        prompt_tokens=10,
        completion_tokens=5,
    )
    store.update_run(run_id, status="awaiting_approval", result="ready")
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id=brief["id"],
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
        build_id=build_id,
        baseline_commit="base",
        candidate_commit="candidate",
        suite_id="development+holdout",
        suite_hash="suite",
        holdout_hash="secret-hash",
        environment_hash="environment",
        repetitions=1,
        budget=budget,
    )

    def result(generation: str, passed: bool) -> CaseResult:
        return CaseResult(
            generation=generation,
            repetition=1,
            case_id="target",
            visibility="holdout",
            process=ProcessResult(
                command=("trusted",),
                returncode=0,
                timed_out=False,
                duration_ms=1,
                stdout="{}",
                stderr="",
                output_truncated=False,
            ),
            observation_hash="hash",
            oracle_passed=passed,
            policy_passed=True,
            failure=None if passed else "oracle",
        )

    baseline_result = result("baseline", False)
    candidate_result = result("candidate", True)
    evaluation = PairedEvaluation(
        baseline_commit="base",
        candidate_commit="candidate",
        development_suite_hash="development",
        holdout_suite_hash="holdout",
        holdout_oracle_hash="oracle",
        environment_hash="environment",
        candidate_patch_hash="patch",
        repetitions=1,
        budget=EvaluationBudget(**budget),
        results=(baseline_result, candidate_result),
        evaluation_id=evaluation_id,
        build_id=build_id,
    )
    trajectory = candidate_trajectory_evidence(
        store.candidate_build_details(build_id), budget
    )
    scorecard = build_scorecard(
        evaluation,
        target_case_ids=["target"],
        trajectory_evidence=trajectory,
    )
    for case in (baseline_result, candidate_result):
        store.add_evaluation_case(
            evaluation_id,
            generation=case.generation,
            repetition=case.repetition,
            case_id=case.case_id,
            visibility=case.visibility,
            result=case.to_dict(redact_holdout=False),
        )
    patch = "diff --git a/runtime/editor.py b/runtime/editor.py\n"
    store.add_evaluation_artifact(
        evaluation_id,
        kind="candidate_patch",
        content_hash=hash_text(patch),
        content=patch,
    )
    trajectory_text = json.dumps(trajectory, sort_keys=True)
    store.add_evaluation_artifact(
        evaluation_id,
        kind="candidate_trajectory",
        content_hash=stable_hash(trajectory),
        content=trajectory_text,
    )
    store.complete_evaluation(
        evaluation_id,
        status="completed",
        scorecard=scorecard.to_dict(),
    )
    store.record_promotion_decision(
        evaluation_id,
        actor="human",
        decision="promote",
        rationale="All predeclared gates passed.",
    )

    assert scorecard.recommendation == "eligible_for_human_promotion"
    assert store.close_campaign_from_evidence(campaign_id) == "completed_clean"
    audit = audit_campaign(StateStore(store.path, read_only=True), campaign_id)
    assert audit.passed is True
    details = store.campaign_details(campaign_id)
    assert details["evaluations"][0]["build_id"] == build_id
    assert details["decisions"][0]["actor"] == "human"

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE evaluation_artifacts SET content = 'tampered'
            WHERE evaluation_id = ? AND kind = 'candidate_patch'
            """,
            (evaluation_id,),
        )
    tampered = audit_campaign(StateStore(store.path, read_only=True), campaign_id)
    assert tampered.passed is False
    assert "hashed_evaluation_artifacts" in {
        check.name for check in tampered.checks if not check.passed
    }


def test_campaign_audit_fails_closed_on_incomplete_campaign(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "campaign.db")
    campaign_id = store.create_campaign(
        baseline_commit="base",
        suite_hash="suite",
        budget={"processes": 1},
        max_candidates=1,
        holdout_hash="holdout",
        environment_hash="environment",
    )

    report = audit_campaign(StateStore(store.path, read_only=True), campaign_id)

    assert report.passed is False
    failed = {check.name for check in report.checks if not check.passed}
    assert "single_human_approved_brief" in failed
    assert "evaluation_lineage" in failed


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
    manifest_path, oracle_path = _test_holdout(tmp_path)
    holdout = load_suite(manifest_path, expected_visibility="holdout")
    oracle, oracle_hash = load_holdout_oracle(oracle_path, holdout)

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


def test_failed_evaluation_retains_patch_and_trajectory_lineage(
    tmp_path: Path,
) -> None:
    if os.environ.get("CANDIDATE_EVALUATION") == "1":
        pytest.skip("Holdout data is intentionally absent from candidate verification.")
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _git_repository(baseline)
    subprocess.run(["git", "clone", "-q", str(baseline), str(candidate)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "evaluation@example.com"],
        cwd=candidate,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evaluation"],
        cwd=candidate,
        check=True,
    )
    (candidate / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=candidate, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=candidate, check=True)

    development = load_suite(
        ROOT / "evaluation" / "suites" / "atomic-v1.json",
        expected_visibility="development",
    )
    manifest_path, oracle_path = _test_holdout(tmp_path)
    holdout = load_suite(manifest_path, expected_visibility="holdout")
    oracle, oracle_hash = load_holdout_oracle(oracle_path, holdout)
    suite_hash = stable_hash(
        {
            "development": development.manifest_hash,
            "holdout": holdout.manifest_hash,
        }
    )
    holdout_hash = stable_hash(
        {"manifest": holdout.manifest_hash, "oracle": oracle_hash}
    )
    _, evaluator_hash = environment_identity(ROOT)
    budget = EvaluationBudget(
        campaign_wall_seconds=30,
        process_wall_seconds=5,
        max_processes=8,
        max_output_bytes=10_000,
        max_memory_mb=256,
        max_file_mb=4,
        max_disk_mb=32,
        max_prompt_tokens=100,
        max_completion_tokens=50,
        max_model_calls=4,
    )
    store = StateStore(tmp_path / "agent.db")
    baseline_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=baseline,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    campaign_id = store.create_campaign(
        baseline_commit=baseline_commit,
        suite_hash=suite_hash,
        budget=asdict(budget),
        max_candidates=1,
        holdout_hash=holdout_hash,
        environment_hash=evaluator_hash,
    )
    brief = {
        "id": "brief-failed-evaluation",
        "evidence_run_ids": ["run"],
        "baseline_commit": baseline_commit,
        "failure_class": "budget",
        "hypothesis": "Bounded execution records terminal failures.",
        "allowed_files": ["tracked.txt"],
        "forbidden_files": ["evaluation/"],
        "acceptance_metrics": [{"case_id": "target"}],
        "suite_hash": suite_hash,
        "budget": asdict(budget),
        "rollback_condition": "Any regression",
    }
    store.add_improvement_brief(campaign_id, brief)
    store.approve_improvement_brief(
        brief["id"], actor="human", rationale="Bounded failure demonstration."
    )
    run_id = store.create_run(
        task="Build candidate",
        mode="agentic",
        repository=candidate,
        base_branch="main",
    )
    build_id = store.create_candidate_build(
        campaign_id,
        brief_id=brief["id"],
        overlay_hash=None,
        overlay=None,
    )
    store.complete_candidate_build(
        build_id,
        run_id=run_id,
        status="awaiting_approval",
        branch="candidate",
        worktree=str(candidate),
    )
    trajectory = {"build_id": build_id, "failures": []}

    with (
        patch("evaluation.supervisor.Supervisor") as supervisor,
        pytest.raises(EvaluationError, match="budget exhausted"),
    ):
        supervisor.return_value.run_path_policy.side_effect = EvaluationError(
            "Evaluation process budget exhausted."
        )
        evaluate_pair(
            trusted_root=ROOT,
            baseline=baseline,
            candidate=candidate,
            development=development,
            holdout=holdout,
            holdout_oracle=oracle,
            holdout_oracle_hash=oracle_hash,
            repetitions=1,
            budget=budget,
            allowed_candidate_paths={"tracked.txt"},
            expected_environment_hash=evaluator_hash,
            state=store,
            campaign_id=campaign_id,
            build_id=build_id,
            trajectory_evidence=trajectory,
        )

    evaluation_id = store.campaign_details(campaign_id)["evaluations"][0]["id"]
    details = store.evaluation_details(evaluation_id)
    assert details is not None
    assert details["status"] == "budget_exhausted"
    assert {artifact["kind"] for artifact in details["artifacts"]} == {
        "candidate_patch",
        "candidate_trajectory",
    }
