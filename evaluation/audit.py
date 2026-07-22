"""Read-only invariant audit for completed recursive-improvement campaigns."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from runtime.state import StateStore, scorecard_allows_promotion

from .outcomes import hash_text, stable_hash

GATE_ORDER = (
    "safety",
    "correctness",
    "regression",
    "control",
    "improvement",
    "efficiency",
)


@dataclass(frozen=True)
class AuditCheck:
    """One deterministic campaign invariant and its bounded evidence."""

    name: str
    passed: bool
    evidence: dict[str, Any]


@dataclass(frozen=True)
class CampaignAudit:
    """Inspection-ready result of a read-only campaign audit."""

    campaign_id: str
    passed: bool
    checks: tuple[AuditCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the audit as JSON-compatible data."""
        return asdict(self)


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _artifact_valid(artifact: dict[str, Any]) -> bool:
    content = artifact.get("content")
    content_hash = artifact.get("content_hash")
    kind = artifact.get("kind")
    if not isinstance(content, str) or not isinstance(content_hash, str):
        return False
    if kind == "candidate_patch":
        return hash_text(content) == content_hash
    if kind == "candidate_trajectory":
        parsed = _json_object(content)
        return parsed is not None and stable_hash(parsed) == content_hash
    return False


def audit_campaign(store: StateStore, campaign_id: str) -> CampaignAudit:
    """Fail closed unless a campaign has complete immutable authorized lineage."""
    campaign = store.campaign_details(campaign_id)
    if campaign is None:
        return CampaignAudit(
            campaign_id=campaign_id,
            passed=False,
            checks=(
                AuditCheck(
                    "campaign_exists",
                    False,
                    {"reason": "unknown_campaign"},
                ),
            ),
        )

    checks: list[AuditCheck] = []
    checks.append(
        AuditCheck(
            "campaign_identity",
            bool(
                campaign.get("baseline_commit")
                and campaign.get("suite_hash")
                and campaign.get("holdout_hash")
                and campaign.get("environment_hash")
                and campaign.get("budget")
            ),
            {
                "baseline_commit": campaign.get("baseline_commit"),
                "suite_hash": campaign.get("suite_hash"),
                "holdout_hash": campaign.get("holdout_hash"),
                "environment_hash": campaign.get("environment_hash"),
            },
        )
    )

    briefs = campaign["briefs"]
    approvals = campaign["brief_approvals"]
    brief = briefs[0] if len(briefs) == 1 else None
    brief_ok = bool(
        brief
        and brief.get("status") == "approved"
        and len(approvals) == 1
        and approvals[0].get("brief_id") == brief.get("id")
        and str(approvals[0].get("actor", "")).strip()
        and str(approvals[0].get("rationale", "")).strip()
        and brief.get("baseline_commit") == campaign.get("baseline_commit")
        and brief.get("suite_hash") == campaign.get("suite_hash")
        and brief.get("budget") == campaign.get("budget")
    )
    checks.append(
        AuditCheck(
            "single_approved_brief",
            brief_ok,
            {"brief_count": len(briefs), "approval_count": len(approvals)},
        )
    )

    builds = campaign["candidate_builds"]
    max_candidates = campaign.get("max_candidates")
    builds_ok = bool(
        brief
        and isinstance(max_candidates, int)
        and 1 <= len(builds) <= max_candidates
        and all(build.get("brief_id") == brief.get("id") for build in builds)
        and all(
            build.get("status")
            in {"awaiting_approval", "needs_attention", "failed", "no_changes"}
            for build in builds
        )
    )
    checks.append(
        AuditCheck(
            "bounded_candidate_builds",
            builds_ok,
            {"build_count": len(builds), "max_candidates": max_candidates},
        )
    )

    evaluations = campaign["evaluations"]
    evaluation_details = [
        store.evaluation_details(evaluation["id"]) for evaluation in evaluations
    ]
    unique_builds = {
        evaluation.get("build_id")
        for evaluation in evaluations
        if evaluation.get("build_id")
    }
    terminal_statuses = {"completed", "failed", "budget_exhausted"}
    evaluated_builds = {evaluation.get("build_id") for evaluation in evaluations}
    build_by_id = {build.get("id"): build for build in builds}
    lineage_ok = bool(
        evaluations
        and len(evaluations) <= max_candidates
        and len(unique_builds) == len(evaluations)
        and all(details is not None for details in evaluation_details)
        and all(
            build_id in build_by_id
            and build_by_id[build_id].get("run_id")
            and build_by_id[build_id].get("worktree")
            for build_id in evaluated_builds
        )
        and all(
            evaluation.get("status") in terminal_statuses
            and evaluation.get("baseline_commit") == campaign.get("baseline_commit")
            and evaluation.get("suite_hash") == campaign.get("suite_hash")
            and evaluation.get("holdout_hash") == campaign.get("holdout_hash")
            and evaluation.get("environment_hash") == campaign.get("environment_hash")
            and evaluation.get("budget") == campaign.get("budget")
            for evaluation in evaluations
        )
    )
    checks.append(
        AuditCheck(
            "evaluation_lineage",
            lineage_ok,
            {
                "evaluation_count": len(evaluations),
                "unique_build_count": len(unique_builds),
            },
        )
    )

    artifact_failures: list[str] = []
    paired_failures: list[str] = []
    scorecard_failures: list[str] = []
    for details in evaluation_details:
        if details is None:
            continue
        evaluation_id = details["id"]
        artifacts_by_kind: dict[str, list[dict[str, Any]]] = {}
        for artifact in details["artifacts"]:
            artifacts_by_kind.setdefault(artifact["kind"], []).append(artifact)
        required_artifacts = ("candidate_patch", "candidate_trajectory")
        if any(
            len(artifacts_by_kind.get(kind, [])) != 1 for kind in required_artifacts
        ):
            artifact_failures.append(evaluation_id)
        elif not all(
            _artifact_valid(artifacts_by_kind[kind][0]) for kind in required_artifacts
        ):
            artifact_failures.append(evaluation_id)

        cases = details["cases"]
        case_keys = {
            (case["repetition"], case["case_id"], case["generation"]) for case in cases
        }
        logical_cases = {(case["repetition"], case["case_id"]) for case in cases}
        completed = details.get("status") == "completed"
        candidate_without_baseline = any(
            generation == "candidate"
            and (repetition, case_id, "baseline") not in case_keys
            for repetition, case_id, generation in case_keys
        )
        incomplete_completed_pair = completed and (
            not cases
            or any(
                (repetition, case_id, generation) not in case_keys
                for repetition, case_id in logical_cases
                for generation in ("baseline", "candidate")
            )
        )
        if candidate_without_baseline or incomplete_completed_pair:
            paired_failures.append(evaluation_id)

        scorecard = _json_object(details.get("scorecard"))
        recommendation = scorecard.get("recommendation") if scorecard else None
        gates = scorecard.get("gates") if scorecard else None
        gate_names = (
            tuple(gate.get("name") for gate in gates)
            if isinstance(gates, list) and all(isinstance(gate, dict) for gate in gates)
            else ()
        )
        terminal_failure_record = bool(
            details.get("status") in {"failed", "budget_exhausted"}
            and scorecard
            and isinstance(scorecard.get("error"), str)
            and isinstance(scorecard.get("message"), str)
        )
        completed_scorecard = bool(
            details.get("status") == "completed"
            and gate_names in (GATE_ORDER, (*GATE_ORDER, "authority"))
            and isinstance(recommendation, str)
        )
        if not terminal_failure_record and not completed_scorecard:
            scorecard_failures.append(evaluation_id)

    checks.append(
        AuditCheck(
            "hashed_evaluation_artifacts",
            not artifact_failures and bool(evaluations),
            {"failed_evaluation_ids": artifact_failures},
        )
    )
    checks.append(
        AuditCheck(
            "paired_case_evidence",
            not paired_failures and bool(evaluations),
            {"failed_evaluation_ids": paired_failures},
        )
    )
    checks.append(
        AuditCheck(
            "lexicographic_scorecards",
            not scorecard_failures and bool(evaluations),
            {"failed_evaluation_ids": scorecard_failures},
        )
    )

    decisions = campaign["decisions"]
    decisions_by_evaluation = {
        decision.get("evaluation_id"): decision for decision in decisions
    }
    decisions_ok = bool(
        evaluations
        and len(decisions) == len(evaluations)
        and all(
            evaluation["id"] in decisions_by_evaluation
            and str(decisions_by_evaluation[evaluation["id"]].get("actor", "")).strip()
            and str(
                decisions_by_evaluation[evaluation["id"]].get("rationale", "")
            ).strip()
            and (
                decisions_by_evaluation[evaluation["id"]].get("decision") != "promote"
                or (
                    evaluation.get("status") == "completed"
                    and scorecard_allows_promotion(
                        _json_object(evaluation.get("scorecard"))
                    )
                )
            )
            for evaluation in evaluations
        )
    )
    checks.append(
        AuditCheck(
            "authorization_decisions",
            decisions_ok,
            {"decision_count": len(decisions)},
        )
    )

    expected_status = "completed_clean" if evaluations else "completed_regression"
    for details in evaluation_details:
        scorecard = _json_object(details.get("scorecard")) if details else None
        gates = (
            {
                gate.get("name"): gate.get("passed")
                for gate in scorecard.get("gates", [])
                if isinstance(gate, dict)
            }
            if scorecard
            else {}
        )
        if (
            not details
            or details.get("status") != "completed"
            or gates.get("safety") is not True
            or gates.get("regression") is not True
        ):
            expected_status = "completed_regression"
            break
    terminal_ok = campaign.get("status") == expected_status
    checks.append(
        AuditCheck(
            "terminal_campaign_status",
            terminal_ok,
            {
                "actual": campaign.get("status"),
                "expected": expected_status,
            },
        )
    )

    return CampaignAudit(
        campaign_id=campaign_id,
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
    )
