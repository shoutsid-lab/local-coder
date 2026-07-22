"""Failure clustering and single-brief campaign controls."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from runtime.state import StateStore

from .outcomes import NormalizedOutcome, stable_hash


class BriefError(ValueError):
    """Raised when a bounded improvement brief cannot be constructed."""


_HYPOTHESES = {
    "budget": (
        "Tighter step and output bounds will prevent the observed budget failure."
    ),
    "editor": "A stricter structured editor handoff will reduce rejected edit batches.",
    "environment": "Explicit environment validation will fail mismatched runs earlier.",
    "manifest": "Strict manifest validation will reject inconsistent evaluation input.",
    "oracle": "A base-owned oracle check will expose the observed correctness failure.",
    "policy": "Earlier policy validation will prevent the observed control violation.",
    "process": "Bounded process handling will record the failure without retry loops.",
    "review": (
        "A stricter review response protocol will reduce invalid review verdicts."
    ),
    "scope": "Earlier scope validation will prevent out-of-scope changes.",
    "tool": "A narrower tool protocol will reduce the observed tool-call failure.",
    "unknown": "Additional structured evidence will make this failure classifiable.",
    "verification": (
        "A focused repair protocol will reduce deterministic test failures."
    ),
}


@dataclass(frozen=True)
class ImprovementBrief:
    """One predeclared, falsifiable candidate proposal."""

    id: str
    evidence_run_ids: tuple[str, ...]
    baseline_commit: str
    failure_class: str
    hypothesis: str
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
    acceptance_metrics: tuple[dict[str, Any], ...]
    suite_hash: str
    budget: dict[str, int]
    rollback_condition: str

    def to_dict(self) -> dict[str, Any]:
        """Return the immutable brief as JSON-compatible data."""
        return asdict(self)


@dataclass(frozen=True)
class ExperimentOverlay:
    """An in-memory prompt or skill variant; never writes candidate files."""

    values: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, values: dict[str, str]) -> "ExperimentOverlay":
        """Validate a small allowlisted overlay mapping."""
        allowed = {
            "planner_handoff",
            "editor_protocol",
            "review_protocol",
            "skill_variant",
        }
        if not values or set(values) - allowed:
            raise BriefError("Experiment overlay contains unsupported keys.")
        if any(not value or len(value) > 8000 for value in values.values()):
            raise BriefError(
                "Experiment overlay values must be bounded non-empty text."
            )
        return cls(tuple(sorted(values.items())))

    @property
    def overlay_hash(self) -> str:
        """Return a stable identity for comparison and lineage."""
        return stable_hash(dict(self.values))


def mine_improvement_brief(
    outcomes: Iterable[NormalizedOutcome],
    *,
    baseline_commit: str,
    allowed_files: Iterable[str],
    forbidden_files: Iterable[str],
    acceptance_metrics: Iterable[dict[str, Any]],
    suite_hash: str,
    budget: dict[str, int],
    rollback_condition: str,
) -> ImprovementBrief:
    """Choose one dominant failure class and emit exactly one strict brief."""
    outcome_list = list(outcomes)
    clusters: dict[str, list[str]] = {}
    for outcome in outcome_list:
        for failure in outcome.failures:
            clusters.setdefault(failure, []).append(outcome.run_id)
    if not clusters:
        raise BriefError("No normalized failures are available to mine.")
    failure_class = sorted(clusters, key=lambda name: (-len(clusters[name]), name))[0]
    allowed = tuple(sorted(set(allowed_files)))
    forbidden = tuple(sorted(set(forbidden_files)))
    metrics = tuple(acceptance_metrics)
    protected_allowed = [
        path
        for path in allowed
        if Path(path).is_absolute()
        or ".." in Path(path).parts
        or Path(path).name.endswith("_contract.py")
        or any(
            path == protected
            or (protected.endswith("/") and path.startswith(protected))
            for protected in forbidden
        )
    ]
    if not allowed or protected_allowed:
        raise BriefError(
            "Allowed files must be non-empty and disjoint from forbidden files."
        )
    if not metrics or any(not isinstance(metric, dict) for metric in metrics):
        raise BriefError("At least one structured acceptance metric is required.")
    if not suite_hash or not rollback_condition.strip():
        raise BriefError("Suite hash and rollback condition are required.")
    if any(not isinstance(value, int) or value <= 0 for value in budget.values()):
        raise BriefError("Brief budget values must be positive integers.")
    evidence_run_ids = tuple(sorted(set(clusters[failure_class])))
    body = {
        "evidence_run_ids": evidence_run_ids,
        "baseline_commit": baseline_commit,
        "failure_class": failure_class,
        "allowed_files": allowed,
        "forbidden_files": forbidden,
        "acceptance_metrics": metrics,
        "suite_hash": suite_hash,
        "budget": budget,
        "rollback_condition": rollback_condition.strip(),
    }
    return ImprovementBrief(
        id=stable_hash(body)[:12],
        hypothesis=_HYPOTHESES.get(failure_class, _HYPOTHESES["unknown"]),
        **body,
    )


def campaign_candidate_limit(store: StateStore) -> int:
    """Allow three iterations only after ten clean campaigns; otherwise one."""
    return 3 if store.completed_clean_campaign_count() >= 10 else 1
