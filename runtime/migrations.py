"""Ordered, additive SQLite migrations for local-coder audit state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA_VERSION = 7


class MigrationError(RuntimeError):
    """Raised when stored schema state is incompatible or incomplete."""


@dataclass(frozen=True)
class Migration:
    """One additive schema transition and its postcondition tables."""

    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        1,
        (
            """CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                mode TEXT NOT NULL, repository TEXT NOT NULL, base_branch TEXT,
                branch TEXT, worktree TEXT, result TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                role TEXT NOT NULL, skill TEXT, model_route TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(run_id, role)
            )""",
            """CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                agent_role TEXT, step_index INTEGER NOT NULL, status TEXT NOT NULL,
                summary TEXT, started_at TEXT NOT NULL, completed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                agent_role TEXT, tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
                output TEXT, status TEXT NOT NULL, duration_ms REAL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, path TEXT, content TEXT, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS verification_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                command TEXT NOT NULL, passed INTEGER NOT NULL, output TEXT NOT NULL,
                duration_ms REAL, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                route TEXT NOT NULL, prompt_tokens INTEGER,
                completion_tokens INTEGER, duration_ms REAL, metadata TEXT,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        2,
        (
            """CREATE TABLE evaluation_campaigns (
                id TEXT PRIMARY KEY, baseline_commit TEXT NOT NULL,
                status TEXT NOT NULL, suite_hash TEXT NOT NULL, budget TEXT NOT NULL,
                max_candidates INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE evaluation_runs (
                id TEXT PRIMARY KEY,
                campaign_id TEXT REFERENCES evaluation_campaigns(id),
                baseline_commit TEXT NOT NULL, candidate_commit TEXT NOT NULL,
                suite_id TEXT NOT NULL, suite_hash TEXT NOT NULL,
                holdout_hash TEXT NOT NULL, environment_hash TEXT NOT NULL,
                repetitions INTEGER NOT NULL, budget TEXT NOT NULL,
                status TEXT NOT NULL, scorecard TEXT, started_at TEXT NOT NULL,
                completed_at TEXT
            )""",
            """CREATE TABLE evaluation_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id TEXT NOT NULL REFERENCES evaluation_runs(id)
                    ON DELETE CASCADE,
                generation TEXT NOT NULL, repetition INTEGER NOT NULL,
                case_id TEXT NOT NULL, visibility TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(evaluation_id, generation, repetition, case_id)
            )""",
            """CREATE TABLE improvement_briefs (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES evaluation_campaigns(id)
                    ON DELETE CASCADE,
                evidence_run_ids TEXT NOT NULL, baseline_commit TEXT NOT NULL,
                failure_class TEXT NOT NULL, hypothesis TEXT NOT NULL,
                allowed_files TEXT NOT NULL, forbidden_files TEXT NOT NULL,
                acceptance_metrics TEXT NOT NULL, suite_hash TEXT NOT NULL,
                budget TEXT NOT NULL, rollback_condition TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(campaign_id)
            )""",
            """CREATE TABLE promotion_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id TEXT NOT NULL REFERENCES evaluation_runs(id)
                    ON DELETE CASCADE,
                actor TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(evaluation_id)
            )""",
        ),
    ),
    Migration(
        3,
        (
            """CREATE TABLE brief_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id TEXT NOT NULL REFERENCES improvement_briefs(id)
                    ON DELETE CASCADE,
                actor TEXT NOT NULL, rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(brief_id)
            )""",
        ),
    ),
    Migration(
        4,
        (
            """CREATE TABLE run_context (
                run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                baseline_commit TEXT, expected_changed_paths TEXT, suite_hash TEXT,
                model_hash TEXT, configuration_hash TEXT, created_at TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        5,
        (
            """CREATE TABLE evaluation_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id TEXT NOT NULL REFERENCES evaluation_runs(id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        6,
        (
            """CREATE TABLE candidate_builds (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES evaluation_campaigns(id)
                    ON DELETE CASCADE,
                brief_id TEXT NOT NULL REFERENCES improvement_briefs(id),
                run_id TEXT REFERENCES runs(id), overlay_hash TEXT, overlay TEXT,
                status TEXT NOT NULL, branch TEXT, worktree TEXT,
                created_at TEXT NOT NULL, completed_at TEXT
            )""",
        ),
    ),
    Migration(
        7,
        (
            """ALTER TABLE evaluation_runs ADD COLUMN build_id TEXT
                REFERENCES candidate_builds(id)""",
            """CREATE UNIQUE INDEX evaluation_runs_build_id
                ON evaluation_runs(build_id)""",
        ),
    ),
)


EXPECTED_COLUMNS = {
    "runs": (
        "id",
        "task",
        "status",
        "mode",
        "repository",
        "base_branch",
        "branch",
        "worktree",
        "result",
        "error",
        "created_at",
        "updated_at",
    ),
    "agents": ("id", "run_id", "role", "skill", "model_route", "status", "created_at"),
    "steps": (
        "id",
        "run_id",
        "agent_role",
        "step_index",
        "status",
        "summary",
        "started_at",
        "completed_at",
    ),
    "tool_calls": (
        "id",
        "run_id",
        "agent_role",
        "tool_name",
        "arguments",
        "output",
        "status",
        "duration_ms",
        "created_at",
    ),
    "artifacts": ("id", "run_id", "kind", "path", "content", "created_at"),
    "verification_results": (
        "id",
        "run_id",
        "command",
        "passed",
        "output",
        "duration_ms",
        "created_at",
    ),
    "model_metrics": (
        "id",
        "run_id",
        "route",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "metadata",
        "created_at",
    ),
    "schema_migrations": ("version", "applied_at"),
    "evaluation_campaigns": (
        "id",
        "baseline_commit",
        "status",
        "suite_hash",
        "budget",
        "max_candidates",
        "created_at",
        "updated_at",
    ),
    "evaluation_runs": (
        "id",
        "campaign_id",
        "baseline_commit",
        "candidate_commit",
        "suite_id",
        "suite_hash",
        "holdout_hash",
        "environment_hash",
        "repetitions",
        "budget",
        "status",
        "scorecard",
        "started_at",
        "completed_at",
        "build_id",
    ),
    "evaluation_cases": (
        "id",
        "evaluation_id",
        "generation",
        "repetition",
        "case_id",
        "visibility",
        "result",
        "created_at",
    ),
    "improvement_briefs": (
        "id",
        "campaign_id",
        "evidence_run_ids",
        "baseline_commit",
        "failure_class",
        "hypothesis",
        "allowed_files",
        "forbidden_files",
        "acceptance_metrics",
        "suite_hash",
        "budget",
        "rollback_condition",
        "status",
        "created_at",
    ),
    "promotion_decisions": (
        "id",
        "evaluation_id",
        "actor",
        "decision",
        "rationale",
        "created_at",
    ),
    "brief_approvals": ("id", "brief_id", "actor", "rationale", "created_at"),
    "run_context": (
        "run_id",
        "baseline_commit",
        "expected_changed_paths",
        "suite_hash",
        "model_hash",
        "configuration_hash",
        "created_at",
    ),
    "evaluation_artifacts": (
        "id",
        "evaluation_id",
        "kind",
        "content_hash",
        "content",
        "created_at",
    ),
    "candidate_builds": (
        "id",
        "campaign_id",
        "brief_id",
        "run_id",
        "overlay_hash",
        "overlay",
        "status",
        "branch",
        "worktree",
        "created_at",
        "completed_at",
    ),
}


TABLE_VERSION = {
    **{
        name: 1
        for name in (
            "runs",
            "agents",
            "steps",
            "tool_calls",
            "artifacts",
            "verification_results",
            "model_metrics",
            "schema_migrations",
        )
    },
    **{
        name: 2
        for name in (
            "evaluation_campaigns",
            "evaluation_runs",
            "evaluation_cases",
            "improvement_briefs",
            "promotion_decisions",
        )
    },
    "brief_approvals": 3,
    "run_context": 4,
    "evaluation_artifacts": 5,
    "candidate_builds": 6,
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("""SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'""")
    return {str(row[0]) for row in rows}


def _reference_schema(version: int) -> sqlite3.Connection:
    """Build the canonical schema shape without consulting stored state."""
    reference = sqlite3.connect(":memory:")
    reference.execute("PRAGMA foreign_keys = ON")
    for migration in MIGRATIONS[:version]:
        for statement in migration.statements:
            reference.execute(statement)
    return reference


def _table_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row[1:]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _foreign_key_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            tuple(row[2:])
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        )
    )


def _index_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    signatures = []
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        columns = tuple(
            item[2] for item in connection.execute(f'PRAGMA index_info("{row[1]}")')
        )
        signatures.append((row[2], row[3], row[4], columns))
    return tuple(sorted(signatures, key=repr))


def _validate(connection: sqlite3.Connection, version: int) -> None:
    tables = _tables(connection)
    expected_tables = {
        name for name, introduced in TABLE_VERSION.items() if introduced <= version
    }
    missing = expected_tables - tables
    if missing:
        raise MigrationError(f"Schema v{version} is missing tables: {sorted(missing)}")
    reference = _reference_schema(version)
    try:
        for table in expected_tables:
            actual_columns = tuple(
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            expected_columns = EXPECTED_COLUMNS[table]
            if table == "evaluation_runs" and version < 7:
                expected_columns = expected_columns[:-1]
            if actual_columns != expected_columns:
                raise MigrationError(
                    f"Schema v{version} has incompatible columns for "
                    f"{table}: {actual_columns}"
                )
            if _table_signature(connection, table) != _table_signature(
                reference, table
            ):
                raise MigrationError(
                    f"Schema v{version} has incompatible column definitions "
                    f"for {table}."
                )
            if _foreign_key_signature(connection, table) != _foreign_key_signature(
                reference, table
            ):
                raise MigrationError(
                    f"Schema v{version} has incompatible foreign keys for {table}."
                )
            if _index_signature(connection, table) != _index_signature(
                reference, table
            ):
                raise MigrationError(
                    f"Schema v{version} has incompatible indexes for {table}."
                )
    finally:
        reference.close()


def _ledger_version(connection: sqlite3.Connection) -> int:
    tables = _tables(connection)
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version > SCHEMA_VERSION:
        raise MigrationError(
            f"Database schema v{user_version} is newer than supported."
        )
    if "schema_migrations" not in tables:
        if user_version != 0:
            raise MigrationError("Database has a version but no migration ledger.")
        return 0
    versions = [
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("Migration ledger is not contiguous.")
    ledger_version = versions[-1] if versions else 0
    if user_version != ledger_version:
        raise MigrationError("PRAGMA user_version and migration ledger disagree.")
    return ledger_version


def migrate(connection: sqlite3.Connection, *, applied_at: str) -> None:
    """Apply every missing migration atomically and fail on incompatible state."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _ledger_version(connection)
        if current:
            _validate(connection, current)
        for migration in MIGRATIONS[current:]:
            for statement in migration.statements:
                connection.execute(statement)
            _validate(connection, migration.version)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (migration.version, applied_at),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise MigrationError(f"Foreign-key violations: {violations[:3]}")
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def applied_schema_version(connection: sqlite3.Connection) -> int:
    """Read schema version without applying migrations."""
    return _ledger_version(connection)
