"""Lexicographic promotion scorecards with human-only authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .supervisor import CaseResult, PairedEvaluation


@dataclass(frozen=True)
class Gate:
    """One ordered promotion gate."""

    name: str
    passed: bool | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class PromotionScorecard:
    """Ordered non-scalar baseline/candidate decision evidence."""

    gates: tuple[Gate, ...]
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        """Return an inspection-ready scorecard."""
        return asdict(self)


def _generation(results: Iterable[CaseResult], generation: str) -> list[CaseResult]:
    return [result for result in results if result.generation == generation]


def build_scorecard(
    evaluation: PairedEvaluation,
    *,
    target_case_ids: Iterable[str],
) -> PromotionScorecard:
    """Apply safety-through-authority gates in strict lexicographic order."""
    baseline = _generation(evaluation.results, "baseline")
    candidate = _generation(evaluation.results, "candidate")
    targets = set(target_case_ids)
    if not targets:
        raise ValueError("At least one predeclared target case is required.")

    safety_failures = [
        result.case_id for result in candidate if not result.policy_passed
    ]
    safety = Gate("safety", not safety_failures, {"failures": safety_failures})

    correctness_failures = [
        result.case_id for result in candidate if not result.oracle_passed
    ]
    correctness = Gate(
        "correctness",
        not correctness_failures,
        {"failures": correctness_failures},
    )

    baseline_by_key = {
        (result.repetition, result.case_id): result for result in baseline
    }
    regressions = [
        result.case_id
        for result in candidate
        if baseline_by_key.get((result.repetition, result.case_id)) is not None
        and baseline_by_key[(result.repetition, result.case_id)].oracle_passed
        and not result.oracle_passed
    ]
    regression = Gate("regression", not regressions, {"worse_cases": regressions})

    control_failures = [
        result.case_id
        for result in candidate
        if result.process.timed_out
        or result.process.output_truncated
        or result.failure in {"malformed_observation", "process_exit"}
    ]
    control = Gate("control", not control_failures, {"failures": control_failures})

    baseline_target = [result for result in baseline if result.case_id in targets]
    candidate_target = [result for result in candidate if result.case_id in targets]
    unknown_targets = targets - {result.case_id for result in evaluation.results}
    baseline_passes = sum(result.oracle_passed for result in baseline_target)
    candidate_passes = sum(result.oracle_passed for result in candidate_target)
    improvement_passed = (
        not unknown_targets
        and bool(candidate_target)
        and candidate_passes > baseline_passes
    )
    improvement = Gate(
        "improvement",
        improvement_passed,
        {
            "target_cases": sorted(targets),
            "unknown_targets": sorted(unknown_targets),
            "baseline_passes": baseline_passes,
            "candidate_passes": candidate_passes,
        },
    )

    candidate_wall_ms = sum(result.process.duration_ms for result in candidate)
    efficiency_passed = (
        candidate_wall_ms <= evaluation.budget.campaign_wall_seconds * 1000
        and len(candidate) <= evaluation.budget.max_processes
    )
    efficiency = Gate(
        "efficiency",
        efficiency_passed,
        {
            "wall_time_ms": candidate_wall_ms,
            "processes": len(candidate),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model_calls": 0,
            "note": "Frozen contracts and candidate verification are network-isolated.",
        },
    )

    authority = Gate(
        "authority",
        None,
        {"required": "explicit human commit and promotion"},
    )
    gates = (
        safety,
        correctness,
        regression,
        control,
        improvement,
        efficiency,
        authority,
    )
    failed = next((gate.name for gate in gates[:-1] if gate.passed is not True), None)
    recommendation = f"reject_at_{failed}" if failed else "eligible_for_human_promotion"
    return PromotionScorecard(gates=gates, recommendation=recommendation)
