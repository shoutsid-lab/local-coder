"""SQLite-backed trajectory and audit logging."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .migrations import SCHEMA_VERSION as MIGRATION_SCHEMA_VERSION
from .migrations import applied_schema_version, migrate

SCHEMA_VERSION = MIGRATION_SCHEMA_VERSION

PROMOTION_GATE_ORDER = (
    "safety",
    "correctness",
    "regression",
    "control",
    "improvement",
    "efficiency",
)


def scorecard_allows_promotion(scorecard: Any) -> bool:
    """Return whether a current or legacy scorecard permits promotion."""
    if not isinstance(scorecard, dict):
        return False
    gates = scorecard.get("gates")
    if not isinstance(gates, list) or not all(isinstance(gate, dict) for gate in gates):
        return False
    names = tuple(gate.get("name") for gate in gates)
    accepted_orders = (PROMOTION_GATE_ORDER, (*PROMOTION_GATE_ORDER, "authority"))
    return names in accepted_orders and all(
        gate.get("passed") is True for gate in gates[: len(PROMOTION_GATE_ORDER)]
    )


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

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not self.read_only:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        connection = self.connect()
        try:
            migrate(connection, applied_at=utc_now())
        finally:
            connection.close()

    def schema_version(self) -> int:
        """Return the newest applied schema migration."""
        with self.connect() as connection:
            return applied_schema_version(connection)

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

    def set_run_context(
        self,
        run_id: str,
        *,
        baseline_commit: str | None,
        expected_changed_paths: list[str] | None,
        suite_hash: str | None,
        model_hash: str | None,
        configuration_hash: str | None,
    ) -> None:
        """Record immutable comparison identity for a newly created run."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_context (
                    run_id, baseline_commit, expected_changed_paths, suite_hash,
                    model_hash, configuration_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    baseline_commit,
                    (
                        json_text(sorted(set(expected_changed_paths)))
                        if expected_changed_paths is not None
                        else None
                    ),
                    suite_hash,
                    model_hash,
                    configuration_hash,
                    utc_now(),
                ),
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

    def start_step(
        self,
        run_id: str,
        *,
        agent_role: str | None,
        summary: str | None = None,
    ) -> int:
        """Start the next ordered step for a run and return its row ID."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            step_index = int(row[0])
            cursor = connection.execute(
                """
                INSERT INTO steps (
                    run_id, agent_role, step_index, status, summary, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, agent_role, step_index, summary, utc_now()),
            )
        return int(cursor.lastrowid)

    def complete_step(
        self,
        step_id: int,
        *,
        status: str,
        summary: str | None = None,
    ) -> None:
        """Complete a previously started step."""
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE steps
                SET status = ?, summary = COALESCE(?, summary), completed_at = ?
                WHERE id = ?
                """,
                (status, summary, utc_now(), step_id),
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

    def tool_call_error_count(
        self,
        run_id: str,
        *,
        tool_name: str | None = None,
    ) -> int:
        """Return the number of recorded failed tool calls for one run."""
        query = "SELECT COUNT(*) FROM tool_calls WHERE run_id = ? AND status = 'error'"
        parameters: list[Any] = [run_id]
        if tool_name is not None:
            query += " AND tool_name = ?"
            parameters.append(tool_name)
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return int(row[0]) if row is not None else 0

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

    def create_campaign(
        self,
        *,
        baseline_commit: str,
        suite_hash: str,
        budget: Any,
        max_candidates: int,
        holdout_hash: str | None = None,
        environment_hash: str | None = None,
        kind: str = "source",
    ) -> str:
        """Create one bounded recursive-improvement campaign."""
        if kind not in {"source", "prompt-optimization"}:
            raise ValueError(f"Unsupported campaign kind: {kind}")
        campaign_id = uuid.uuid4().hex[:12]
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_campaigns (
                    id, baseline_commit, status, suite_hash, budget,
                    max_candidates, created_at, updated_at,
                    holdout_hash, environment_hash, kind
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    baseline_commit,
                    suite_hash,
                    json_text(budget),
                    max_candidates,
                    timestamp,
                    timestamp,
                    holdout_hash,
                    environment_hash,
                    kind,
                ),
            )
        return campaign_id

    def completed_clean_campaign_count(self) -> int:
        """Return campaigns closed without a safety regression."""
        with self.connect() as connection:
            row = connection.execute("""
                SELECT COUNT(*) FROM evaluation_campaigns
                WHERE status = 'completed_clean'
                """).fetchone()
        return int(row[0]) if row is not None else 0

    def campaign_details(self, campaign_id: str) -> dict[str, Any] | None:
        """Return a campaign with briefs, evaluations, and authorization decisions."""
        with self.connect() as connection:
            campaign = connection.execute(
                "SELECT * FROM evaluation_campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if campaign is None:
                return None
            result = dict(campaign)
            result["briefs"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM improvement_briefs
                    WHERE campaign_id = ? ORDER BY created_at
                    """,
                    (campaign_id,),
                )
            ]
            result["candidate_builds"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM candidate_builds
                    WHERE campaign_id = ? ORDER BY created_at
                    """,
                    (campaign_id,),
                )
            ]
            result["candidate_artifacts"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT artifact.* FROM candidate_artifacts AS artifact
                    JOIN candidate_builds AS build ON build.id = artifact.build_id
                    WHERE build.campaign_id = ?
                    ORDER BY artifact.created_at, artifact.id
                    """,
                    (campaign_id,),
                )
            ]
            result["evaluations"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM evaluation_runs
                    WHERE campaign_id = ? ORDER BY started_at
                    """,
                    (campaign_id,),
                )
            ]
            result["evaluation_artifacts"] = []
            for evaluation in result["evaluations"]:
                result["evaluation_artifacts"].extend(
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT id, evaluation_id, kind, content_hash, created_at
                        FROM evaluation_artifacts
                        WHERE evaluation_id = ? ORDER BY created_at
                        """,
                        (evaluation["id"],),
                    )
                )
            evaluation_ids = [row["id"] for row in result["evaluations"]]
            result["decisions"] = []
            result["brief_approvals"] = []
            for brief in result["briefs"]:
                result["brief_approvals"].extend(
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM brief_approvals
                        WHERE brief_id = ? ORDER BY created_at
                        """,
                        (brief["id"],),
                    )
                )
            for evaluation_id in evaluation_ids:
                result["decisions"].extend(
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM promotion_decisions
                        WHERE evaluation_id = ? ORDER BY created_at
                        """,
                        (evaluation_id,),
                    )
                )
        return result

    def update_campaign_status(self, campaign_id: str, status: str) -> None:
        """Set a terminal campaign audit status without changing Git state."""
        if status not in {"completed_clean", "completed_regression", "cancelled"}:
            raise ValueError(f"Unsupported campaign status: {status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE evaluation_campaigns SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, utc_now(), campaign_id),
            )

    def close_campaign_from_evidence(self, campaign_id: str) -> str:
        """Close a decided campaign and derive cleanliness from stored scorecards."""
        details = self.campaign_details(campaign_id)
        if details is None or details["status"] != "active":
            raise ValueError("Campaign is missing or not active.")
        evaluations = details["evaluations"]
        if not evaluations or len(details["decisions"]) != len(evaluations):
            raise ValueError(
                "Every campaign evaluation requires an authorization decision."
            )
        clean = True
        for evaluation in evaluations:
            if evaluation["status"] != "completed" or not evaluation["scorecard"]:
                clean = False
                continue
            try:
                scorecard = json.loads(evaluation["scorecard"])
                gates = {gate["name"]: gate["passed"] for gate in scorecard["gates"]}
            except (json.JSONDecodeError, KeyError, TypeError):
                clean = False
                continue
            clean = (
                clean
                and gates.get("safety") is True
                and gates.get("regression") is True
            )
        status = "completed_clean" if clean else "completed_regression"
        self.update_campaign_status(campaign_id, status)
        return status

    def add_improvement_brief(self, campaign_id: str, brief: dict[str, Any]) -> str:
        """Persist the campaign's single predeclared improvement brief."""
        brief_id = str(brief["id"])
        with self.connect() as connection:
            campaign = connection.execute(
                "SELECT kind FROM evaluation_campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            if campaign is None:
                raise ValueError("Campaign is missing.")
            metadata = brief.get("metadata")
            if campaign["kind"] == "prompt-optimization":
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("campaign_kind") != "prompt-optimization"
                ):
                    raise ValueError(
                        "Prompt campaign briefs require matching typed metadata."
                    )
            connection.execute(
                """
                INSERT INTO improvement_briefs (
                    id, campaign_id, evidence_run_ids, baseline_commit,
                    failure_class, hypothesis, allowed_files, forbidden_files,
                    acceptance_metrics, suite_hash, budget, rollback_condition,
                    status, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)
                """,
                (
                    brief_id,
                    campaign_id,
                    json_text(brief["evidence_run_ids"]),
                    brief["baseline_commit"],
                    brief["failure_class"],
                    brief["hypothesis"],
                    json_text(brief["allowed_files"]),
                    json_text(brief["forbidden_files"]),
                    json_text(brief["acceptance_metrics"]),
                    brief["suite_hash"],
                    json_text(brief["budget"]),
                    brief["rollback_condition"],
                    utc_now(),
                    json_text(brief["metadata"]) if brief.get("metadata") else None,
                ),
            )
        return brief_id

    def approve_improvement_brief(
        self,
        brief_id: str,
        *,
        actor: str,
        rationale: str,
    ) -> None:
        """Record explicit actor authorization to evaluate one brief."""
        if not actor.strip() or not rationale.strip():
            raise ValueError("An actor and rationale are required.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE improvement_briefs SET status = 'approved'
                WHERE id = ? AND status = 'pending_approval'
                """,
                (brief_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Brief is missing or is not pending approval.")
            connection.execute(
                """
                INSERT INTO brief_approvals (brief_id, actor, rationale, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (brief_id, actor.strip(), rationale.strip(), utc_now()),
            )

    def create_candidate_build(
        self,
        campaign_id: str,
        *,
        brief_id: str,
        overlay_hash: str | None,
        overlay: Any,
        build_kind: str = "source",
    ) -> str:
        """Reserve one bounded candidate-build attempt for an approved brief."""
        if build_kind not in {"source", "prompt-optimization"}:
            raise ValueError(f"Unsupported candidate-build kind: {build_kind}")
        build_id = uuid.uuid4().hex[:12]
        with self.connect() as connection:
            campaign = connection.execute(
                """
                SELECT status, max_candidates, kind
                FROM evaluation_campaigns WHERE id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if campaign is None or campaign["status"] != "active":
                raise ValueError("Campaign is missing or not active.")
            if campaign["kind"] != build_kind:
                raise ValueError("Candidate-build kind does not match the campaign.")
            brief = connection.execute(
                """
                SELECT status FROM improvement_briefs
                WHERE id = ? AND campaign_id = ?
                """,
                (brief_id, campaign_id),
            ).fetchone()
            if brief is None or brief["status"] != "approved":
                raise ValueError(
                    "Candidate build requires the approved campaign brief."
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM candidate_builds WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()[0]
            if count >= campaign["max_candidates"]:
                raise ValueError("Campaign candidate-build limit has been reached.")
            connection.execute(
                """
                INSERT INTO candidate_builds (
                    id, campaign_id, brief_id, overlay_hash, overlay,
                    status, created_at, build_kind
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    build_id,
                    campaign_id,
                    brief_id,
                    overlay_hash,
                    json_text(overlay) if overlay is not None else None,
                    utc_now(),
                    build_kind,
                ),
            )
        return build_id

    def complete_candidate_build(
        self,
        build_id: str,
        *,
        run_id: str | None,
        status: str,
        branch: str | None,
        worktree: str | None,
    ) -> None:
        """Attach the uncommitted agent run to its campaign build record."""
        if status not in {
            "awaiting_approval",
            "needs_attention",
            "failed",
            "no_changes",
            "candidate_ready",
        }:
            raise ValueError(f"Unsupported candidate-build status: {status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE candidate_builds
                SET run_id = ?, status = ?, branch = ?, worktree = ?, completed_at = ?
                WHERE id = ?
                """,
                (run_id, status, branch, worktree, utc_now(), build_id),
            )

    def candidate_build_details(self, build_id: str) -> dict[str, Any] | None:
        """Return one candidate build and its audited run trajectory."""
        with self.connect() as connection:
            build = connection.execute(
                "SELECT * FROM candidate_builds WHERE id = ?", (build_id,)
            ).fetchone()
        if build is None:
            return None
        result = dict(build)
        result["run"] = self.run_details(result["run_id"]) if result["run_id"] else None
        with self.connect() as connection:
            result["artifacts"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM candidate_artifacts
                    WHERE build_id = ? ORDER BY created_at, id
                    """,
                    (build_id,),
                )
            ]
        return result

    def add_candidate_artifact(
        self,
        build_id: str,
        *,
        kind: str,
        path: str | None,
        content_hash: str,
        content: str,
    ) -> int:
        """Persist one immutable hash-bound candidate-build artifact."""
        if not kind.strip() or not content_hash.strip() or not content:
            raise ValueError("Candidate artifact kind, hash, and content are required.")
        with self.connect() as connection:
            build = connection.execute(
                "SELECT status FROM candidate_builds WHERE id = ?", (build_id,)
            ).fetchone()
            if build is None or build["status"] != "running":
                raise ValueError("Candidate artifact requires a running build.")
            cursor = connection.execute(
                """
                INSERT INTO candidate_artifacts (
                    build_id, kind, path, content_hash, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (build_id, kind.strip(), path, content_hash, content, utc_now()),
            )
        return int(cursor.lastrowid)

    def evaluation_details(self, evaluation_id: str) -> dict[str, Any] | None:
        """Return one evaluation with cases, artifacts, and authorization."""
        with self.connect() as connection:
            evaluation = connection.execute(
                "SELECT * FROM evaluation_runs WHERE id = ?", (evaluation_id,)
            ).fetchone()
            if evaluation is None:
                return None
            result = dict(evaluation)
            result["cases"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM evaluation_cases
                    WHERE evaluation_id = ?
                    ORDER BY repetition, case_id, generation
                    """,
                    (evaluation_id,),
                )
            ]
            result["artifacts"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM evaluation_artifacts
                    WHERE evaluation_id = ? ORDER BY created_at, id
                    """,
                    (evaluation_id,),
                )
            ]
            decision = connection.execute(
                """
                SELECT * FROM promotion_decisions
                WHERE evaluation_id = ?
                """,
                (evaluation_id,),
            ).fetchone()
            result["decision"] = dict(decision) if decision is not None else None
        return result

    def create_evaluation(
        self,
        *,
        campaign_id: str | None,
        baseline_commit: str,
        candidate_commit: str,
        suite_id: str,
        suite_hash: str,
        holdout_hash: str,
        environment_hash: str,
        repetitions: int,
        budget: Any,
        build_id: str | None = None,
    ) -> str:
        """Create one immutable evaluation-lineage record."""
        evaluation_id = uuid.uuid4().hex[:12]
        with self.connect() as connection:
            if campaign_id is not None:
                campaign = connection.execute(
                    """
                    SELECT status, max_candidates, baseline_commit, suite_hash, budget,
                           holdout_hash, environment_hash
                    FROM evaluation_campaigns WHERE id = ?
                    """,
                    (campaign_id,),
                ).fetchone()
                if campaign is None or campaign["status"] != "active":
                    raise ValueError("Campaign is missing or not active.")
                approved = connection.execute(
                    """
                    SELECT COUNT(*) FROM improvement_briefs
                    WHERE campaign_id = ? AND status = 'approved'
                    """,
                    (campaign_id,),
                ).fetchone()[0]
                if approved != 1:
                    raise ValueError("Campaign requires one approved brief.")
                if build_id is None:
                    raise ValueError(
                        "Campaign evaluation requires a candidate build ID."
                    )
                build = connection.execute(
                    """
                    SELECT candidate_builds.status, candidate_builds.run_id,
                           candidate_builds.worktree,
                           improvement_briefs.status AS brief_status
                    FROM candidate_builds
                    JOIN improvement_briefs
                      ON improvement_briefs.id = candidate_builds.brief_id
                    WHERE candidate_builds.id = ?
                      AND candidate_builds.campaign_id = ?
                    """,
                    (build_id, campaign_id),
                ).fetchone()
                if build is None:
                    raise ValueError("Candidate build does not belong to the campaign.")
                if (
                    build["status"] not in {"awaiting_approval", "needs_attention"}
                    or build["run_id"] is None
                    or build["worktree"] is None
                    or build["brief_status"] != "approved"
                ):
                    raise ValueError(
                        "Candidate build has no evaluable audited trajectory."
                    )
                if campaign["baseline_commit"] != baseline_commit:
                    raise ValueError("Evaluation baseline differs from the campaign.")
                if campaign["suite_hash"] != suite_hash:
                    raise ValueError("Evaluation suite differs from the campaign.")
                if campaign["budget"] != json_text(budget):
                    raise ValueError("Evaluation budget differs from the campaign.")
                if not campaign["holdout_hash"] or not campaign["environment_hash"]:
                    raise ValueError(
                        "Campaign lacks frozen holdout or environment identity."
                    )
                if campaign["holdout_hash"] != holdout_hash:
                    raise ValueError("Evaluation holdout differs from the campaign.")
                if campaign["environment_hash"] != environment_hash:
                    raise ValueError(
                        "Evaluation environment differs from the campaign."
                    )
                count = connection.execute(
                    "SELECT COUNT(*) FROM evaluation_runs WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()[0]
                if count >= campaign["max_candidates"]:
                    raise ValueError("Campaign candidate limit has been reached.")
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                    id, campaign_id, build_id, baseline_commit,
                    candidate_commit, suite_id,
                    suite_hash, holdout_hash, environment_hash, repetitions,
                    budget, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    evaluation_id,
                    campaign_id,
                    build_id,
                    baseline_commit,
                    candidate_commit,
                    suite_id,
                    suite_hash,
                    holdout_hash,
                    environment_hash,
                    repetitions,
                    json_text(budget),
                    utc_now(),
                ),
            )
        return evaluation_id

    def add_evaluation_case(
        self,
        evaluation_id: str,
        *,
        generation: str,
        repetition: int,
        case_id: str,
        visibility: str,
        result: Any,
    ) -> None:
        """Persist one paired case result."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_cases (
                    evaluation_id, generation, repetition, case_id,
                    visibility, result, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    generation,
                    repetition,
                    case_id,
                    visibility,
                    json_text(result),
                    utc_now(),
                ),
            )

    def add_evaluation_artifact(
        self,
        evaluation_id: str,
        *,
        kind: str,
        content_hash: str,
        content: str,
    ) -> None:
        """Archive a trusted evaluation artifact and its verified hash."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_artifacts (
                    evaluation_id, kind, content_hash, content, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (evaluation_id, kind, content_hash, content, utc_now()),
            )

    def complete_evaluation(
        self,
        evaluation_id: str,
        *,
        status: str,
        scorecard: Any,
    ) -> None:
        """Finish an evaluation with its lexicographic scorecard."""
        if status not in {"completed", "failed", "budget_exhausted"}:
            raise ValueError(f"Unsupported evaluation status: {status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE evaluation_runs
                SET status = ?, scorecard = ?, completed_at = ? WHERE id = ?
                """,
                (status, json_text(scorecard), utc_now(), evaluation_id),
            )

    def record_promotion_decision(
        self,
        evaluation_id: str,
        *,
        actor: str,
        decision: str,
        rationale: str,
    ) -> None:
        """Record an actor decision without performing any promotion action."""
        if decision not in {"promote", "reject"}:
            raise ValueError("Decision must be 'promote' or 'reject'.")
        if not actor.strip() or not rationale.strip():
            raise ValueError("An actor and rationale are required.")
        with self.connect() as connection:
            evaluation = connection.execute(
                "SELECT status, scorecard FROM evaluation_runs WHERE id = ?",
                (evaluation_id,),
            ).fetchone()
            if evaluation is None or evaluation["status"] == "running":
                raise ValueError("Evaluation is missing or not terminal.")
            if decision == "promote":
                try:
                    scorecard = json.loads(evaluation["scorecard"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("Promotion requires a valid scorecard.") from exc
                if not scorecard_allows_promotion(scorecard):
                    raise ValueError("Promotion recommendation gates did not pass.")
            connection.execute(
                """
                INSERT INTO promotion_decisions (
                    evaluation_id, actor, decision, rationale, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (evaluation_id, actor.strip(), decision, rationale.strip(), utc_now()),
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
            try:
                context = connection.execute(
                    "SELECT * FROM run_context WHERE run_id = ?", (run_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                context = None
            result["context"] = dict(context) if context is not None else None
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
            result["steps"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,)
                )
            ]
            result["artifacts"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM artifacts WHERE run_id = ? ORDER BY id", (run_id,)
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
            result["model_metrics"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM model_metrics
                    WHERE run_id = ? ORDER BY id
                    """,
                    (run_id,),
                )
            ]
        return result
