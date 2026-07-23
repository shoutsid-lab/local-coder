"""Decision, close, audit, and optional activation for prompt campaigns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.audit import audit_campaign
from runtime.prompt_activation import (
    PromptActivationError,
    activate_prompt_candidate,
)
from runtime.state import StateStore, scorecard_allows_promotion


class PromptDeploymentError(ValueError):
    """Raised when a prompt campaign cannot be finalized safely."""


def _scorecard(evaluation: dict[str, Any]) -> dict[str, Any]:
    raw = evaluation.get("scorecard")
    if not isinstance(raw, str):
        raise PromptDeploymentError("Prompt evaluation has no scorecard.")
    try:
        scorecard = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromptDeploymentError("Prompt evaluation scorecard is invalid.") from exc
    if not isinstance(scorecard, dict):
        raise PromptDeploymentError("Prompt evaluation scorecard must be an object.")
    return scorecard


def finalize_prompt_campaign(
    store: StateStore,
    campaign_id: str,
    *,
    actor: str,
    rationale: str,
    activate: bool,
    repository_root: Path,
    prompt_store_root: Path | None = None,
) -> dict[str, Any]:
    """Record the scorecard-derived decision, close, audit, and optionally activate."""
    if not actor.strip() or not rationale.strip():
        raise PromptDeploymentError("Finalization requires an actor and rationale.")
    campaign = store.campaign_details(campaign_id)
    if campaign is None:
        raise PromptDeploymentError("Prompt campaign is missing.")
    if campaign.get("kind") != "prompt-optimization":
        raise PromptDeploymentError("Only prompt campaigns use this finalizer.")
    evaluations = campaign.get("evaluations")
    if not isinstance(evaluations, list) or len(evaluations) != 1:
        raise PromptDeploymentError(
            "Prompt campaign finalization requires exactly one evaluation."
        )
    evaluation_id = evaluations[0].get("id")
    if not isinstance(evaluation_id, str):
        raise PromptDeploymentError("Prompt evaluation ID is missing.")
    evaluation = store.evaluation_details(evaluation_id)
    if evaluation is None or evaluation.get("status") != "completed":
        raise PromptDeploymentError("Prompt evaluation is not completed.")
    scorecard = _scorecard(evaluation)
    decision_name = "promote" if scorecard_allows_promotion(scorecard) else "reject"
    existing = evaluation.get("decision")
    if existing is None:
        store.record_promotion_decision(
            evaluation_id,
            actor=actor,
            decision=decision_name,
            rationale=rationale,
        )
    elif existing.get("decision") != decision_name:
        raise PromptDeploymentError(
            "Stored prompt decision conflicts with the frozen scorecard."
        )

    campaign = store.campaign_details(campaign_id)
    if campaign is None:
        raise PromptDeploymentError("Prompt campaign disappeared during finalization.")
    status = str(campaign.get("status"))
    if status == "active":
        status = store.close_campaign_from_evidence(campaign_id)
    elif status not in {"completed_clean", "completed_regression"}:
        raise PromptDeploymentError(f"Unsupported prompt campaign status: {status}")

    audit = audit_campaign(StateStore(store.path, read_only=True), campaign_id)
    if not audit.passed:
        raise PromptDeploymentError("Prompt campaign audit failed after close.")

    activation = None
    activation_status = "not_requested"
    if activate and decision_name == "promote" and status == "completed_clean":
        try:
            activation = activate_prompt_candidate(
                store,
                evaluation_id,
                actor=actor,
                rationale=rationale,
                repository_root=repository_root,
                store_root=prompt_store_root,
            )
        except PromptActivationError as exc:
            raise PromptDeploymentError(str(exc)) from exc
        activation_status = "active"
    elif activate:
        activation_status = "skipped_not_eligible"

    return {
        "campaign_id": campaign_id,
        "evaluation_id": evaluation_id,
        "decision": decision_name,
        "status": status,
        "audit": audit.to_dict(),
        "activation": activation,
        "activation_status": activation_status,
    }
