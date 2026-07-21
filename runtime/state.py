"""SQLite-backed trajectory and audit logging."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """Return the current UTC time in an ISO-8601 representation."""
    return datetime.now(UTC).isoformat()


def json_text(value: Any) -> str:
    """Serialize a value for durable storage."""
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, default=str, sort_keys=True)


class StateStore:
    """Persist agent runs, tool calls, verification, and model metrics."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            repository TEXT NOT NULL,
            base_branch TEXT,
            branch TEXT,
            worktree TEXT,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            skill TEXT,
            model_route TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, role)
        );

        CREATE TABLE IF NOT EXISTS steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            agent_role TEXT,
            step_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            agent_role TEXT,
            tool_name TEXT NOT NULL,
            arguments TEXT NOT NULL,
            output TEXT,
            status TEXT NOT NULL,
            duration_ms REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path TEXT,
            content TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS verification_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            command TEXT NOT NULL,
            passed INTEGER NOT NULL,
            output TEXT NOT NULL,
            duration_ms REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            route TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            duration_ms REAL,
            metadata TEXT,
            created_at TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)

    def create_run(
        self,
        *,
        task: str,
        mode: str,
        repository: Path,
        base_branch: str | None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, task, status, mode, repository, base_branch,
                    created_at, updated_at
                ) VALUES (?, ?, 'created', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task,
                    mode,
                    str(repository),
                    base_branch,
                    timestamp,
                    timestamp,
                ),
            )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "branch",
            "worktree",
            "result",
            "error",
            "base_branch",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {sorted(unknown)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [
            json_text(value) if name == "result" and value is not None else value
            for name, value in fields.items()
        ]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",  # noqa: S608
                [*values, run_id],
            )

    def register_agent(
        self,
        run_id: str,
        *,
        role: str,
        skill: str | None,
        model_route: str,
        status: str = "ready",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO agents (
                    run_id, role, skill, model_route, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, role, skill, model_route, status, utc_now()),
            )

    def log_tool_call(
        self,
        run_id: str,
        *,
        agent_role: str | None,
        tool_name: str,
        arguments: Any,
        output: str | None,
        status: str,
        duration_ms: float | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls (
                    run_id, agent_role, tool_name, arguments, output,
                    status, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_role,
                    tool_name,
                    json_text(arguments),
                    output,
                    status,
                    duration_ms,
                    utc_now(),
                ),
            )

    def add_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        path: Path | None = None,
        content: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (run_id, kind, path, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    str(path) if path else None,
                    content,
                    utc_now(),
                ),
            )

    def add_verification(
        self,
        run_id: str,
        *,
        command: str,
        passed: bool,
        output: str,
        duration_ms: float | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO verification_results (
                    run_id, command, passed, output, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    command,
                    int(passed),
                    output,
                    duration_ms,
                    utc_now(),
                ),
            )

    def add_model_metrics(
        self,
        run_id: str,
        *,
        route: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        duration_ms: float | None = None,
        metadata: Any = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_metrics (
                    run_id, route, prompt_tokens, completion_tokens,
                    duration_ms, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    route,
                    prompt_tokens,
                    completion_tokens,
                    duration_ms,
                    json_text(metadata) if metadata is not None else None,
                    utc_now(),
                ),
            )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task, status, mode, branch, worktree,
                       created_at, updated_at
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_details(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            result = dict(run)
            result["agents"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM agents WHERE run_id = ? ORDER BY id", (run_id,)
                )
            ]
            result["tool_calls"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id", (run_id,)
                )
            ]
            result["verification"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM verification_results
                    WHERE run_id = ? ORDER BY id
                    """,
                    (run_id,),
                )
            ]
        return result
