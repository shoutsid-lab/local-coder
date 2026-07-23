"""Offline, non-promoting GEPA optimization runner for one DSPy role."""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from evaluation.outcomes import stable_hash
from runtime.dspy_lm import DSPY_ROUTES, build_dspy_lm

from .gepa_dataset import GepaDatasetError, load_gepa_dataset

GEPA_RUN_SCHEMA_VERSION = 3
DEFAULT_MAX_METRIC_CALLS = 60
DEFAULT_MAX_UNSAFE_PROPOSALS = 3
DEFAULT_NO_IMPROVEMENT_PATIENCE = 6
DEFAULT_REFLECTION_MAX_TOKENS = 512
DEFAULT_MAX_INSTRUCTION_CHARS = 1600
AUTO_CANDIDATES = {"light": 6, "medium": 12, "heavy": 18}
REFLECTION_GUARD = (
    "Reflection constraints: ground proposals only in the supplied task, typed "
    "inputs, repository evidence, and audit feedback. Do not invent occurrence "
    "counts, filesystem metadata, or unstated constraints. Prefer a concise "
    "role-level instruction over replaying the example verbatim."
)
FORBIDDEN_CANDIDATE_MARKERS = (
    "# new inputs",
    "## inputs",
    "### task",
    "### repository_evidence",
    "## generated outputs",
    "## feedback",
    "## additional information",
    "gepa corpus case:",
)
ROLE_INPUT_FIELDS = {
    "explorer": ("task", "delegated_task", "repository_evidence"),
    "planner": ("task", "delegated_task", "repository_evidence"),
    "implementer": ("task", "instruction", "editable_files", "file_contents"),
    "repairer": (
        "task",
        "delegated_task",
        "verification_output",
        "diff",
        "editable_files",
        "file_contents",
    ),
    "reviewer": (
        "task",
        "changed_files",
        "verification_passed",
        "verification_output",
        "diff",
    ),
}
ROLE_OUTPUT_FIELDS = {
    "explorer": (
        "findings",
        "relevant_files",
        "constraints",
        "unresolved_questions",
    ),
    "planner": (
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
    ),
    "implementer": ("edits",),
    "repairer": ("diagnosis", "edits"),
    "reviewer": ("verdict", "summary", "issues", "unrelated_changes"),
}
ROLE_ROUTES = {
    "explorer": "local-plan",
    "planner": "local-plan",
    "implementer": "local-fast",
    "repairer": "local-fast",
    "reviewer": "local-review",
}


class GepaRunnerError(ValueError):
    """Raised when an offline optimization run cannot proceed safely."""


class ModelCallBudgetExceeded(RuntimeError):
    """Raised before a campaign-visible model call would exceed its hard limit."""


class ModelCallBudget:
    """Count and enforce student and reflection model calls across one run."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.counts: Counter[str] = Counter()
        self.blocked_calls = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def at_limit(self) -> bool:
        return self.limit is not None and self.total >= self.limit

    def consume(self, category: str) -> None:
        if self.at_limit:
            self.blocked_calls += 1
            raise ModelCallBudgetExceeded(
                f"Hard model-call limit reached before {category} call: "
                f"{self.total}/{self.limit}."
            )
        self.counts[category] += 1

    def summary(self) -> dict[str, Any]:
        return {
            "hard_limit": self.limit,
            "student": self.counts["student"],
            "reflection": self.counts["reflection"],
            "total": self.total,
            "at_limit": self.at_limit,
            "blocked_calls": self.blocked_calls,
            "provider_retries_included": False,
        }


class BudgetedLM:
    """Transparent LM proxy that shares one strict call budget across copies."""

    def __init__(self, lm: Any, budget: ModelCallBudget, category: str) -> None:
        self._lm = lm
        self._budget = budget
        self._category = category

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lm, name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "BudgetedLM":
        del memo
        return self

    def copy(self, **kwargs: Any) -> "BudgetedLM":
        """Preserve the shared hard budget across DSPy LM runtime copies."""
        method = getattr(self._lm, "copy", None)
        copied = method(**kwargs) if callable(method) else self._lm
        return BudgetedLM(copied, self._budget, self._category)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._budget.consume(self._category)
        return self._lm(*args, **kwargs)

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        self._budget.consume(self._category)
        method = getattr(self._lm, "acall")
        return await method(*args, **kwargs)


class BoundedNoImprovementStopper:
    """Stop on target calls, hard LM limits, or repeated weak proposals."""

    def __init__(
        self,
        target_metric_calls: int,
        patience: int | None,
        *,
        proposal_guard: Any | None = None,
        model_budget: ModelCallBudget | None = None,
    ) -> None:
        self.target_metric_calls = target_metric_calls
        self.patience = patience
        self.proposal_guard = proposal_guard
        self.model_budget = model_budget
        self.best_score = float("-inf")
        self.iterations_without_improvement = 0
        self.last_iteration: int | None = None
        self.reason: str | None = None

    def __call__(self, state: Any) -> bool:
        if self.model_budget is not None and self.model_budget.at_limit:
            self.reason = "hard_model_call_limit"
            return True
        if bool(getattr(self.proposal_guard, "stop_requested", False)):
            self.reason = "unsafe_proposal_limit"
            return True
        total = int(getattr(state, "total_num_evals", 0) or 0)
        if total >= self.target_metric_calls:
            self.reason = "target_metric_calls"
            return True
        if self.patience is None:
            return False
        iteration = getattr(state, "i", None)
        if iteration is None or iteration == self.last_iteration:
            return False
        self.last_iteration = int(iteration)
        scores = getattr(state, "program_full_scores_val_set", None) or []
        current = max(float(value) for value in scores) if scores else 0.0
        if current > self.best_score:
            self.best_score = current
            self.iterations_without_improvement = 0
        else:
            self.iterations_without_improvement += 1
        if self.iterations_without_improvement >= self.patience:
            self.reason = "no_improvement"
            return True
        return False

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.patience is not None,
            "patience": self.patience,
            "target_metric_calls": self.target_metric_calls,
            "reason": self.reason,
            "iterations_without_improvement": self.iterations_without_improvement,
            "best_observed_score": (
                None if self.best_score == float("-inf") else self.best_score
            ),
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prediction_value(prediction: Any, field: str) -> Any:
    if isinstance(prediction, Mapping):
        return prediction.get(field)
    return getattr(prediction, field, None)


def _field_similarity(expected: Any, actual: Any) -> float:
    if expected == actual:
        return 1.0
    if type(expected) is not type(actual):
        return 0.0
    if isinstance(expected, str):
        return difflib.SequenceMatcher(None, expected, actual).ratio()
    if isinstance(expected, list):
        expected_text = _canonical_json(expected)
        actual_text = _canonical_json(actual)
        return difflib.SequenceMatcher(None, expected_text, actual_text).ratio()
    if isinstance(expected, dict):
        expected_text = _canonical_json(expected)
        actual_text = _canonical_json(actual)
        return difflib.SequenceMatcher(None, expected_text, actual_text).ratio()
    return 0.0


def build_gepa_metric(role: str, *, dspy_module: Any) -> Callable[..., Any]:
    """Build a deterministic replay metric with textual audit feedback."""
    if role not in ROLE_OUTPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    output_fields = ROLE_OUTPUT_FIELDS[role]

    def metric(
        gold: Any,
        pred: Any,
        trace: Any = None,
        pred_name: str | None = None,
        pred_trace: Any = None,
    ) -> Any:
        del trace, pred_name, pred_trace
        similarities: list[float] = []
        mismatches: list[str] = []
        for field in output_fields:
            expected = getattr(gold, f"expected_{field}")
            actual = _prediction_value(pred, field)
            similarity = _field_similarity(expected, actual)
            similarities.append(similarity)
            if similarity < 1.0:
                mismatches.append(field)
        structural_score = mean(similarities) if similarities else 0.0
        audit_score = float(getattr(gold, "audit_score", 0.0))
        score = structural_score * (0.5 + 0.5 * audit_score)
        mismatch_text = (
            ", ".join(mismatches) if mismatches else "none; output matched the audit"
        )
        feedback = (
            f"Audited replay for role {role}. Mismatched fields: {mismatch_text}. "
            f"Historical outcome score: {audit_score:.1f}. "
            f"Reviewer feedback: {getattr(gold, 'audit_feedback', '')} "
            f"{REFLECTION_GUARD}"
        )
        prediction_type = getattr(dspy_module, "Prediction", None)
        if prediction_type is None:
            return {"score": score, "feedback": feedback}
        return prediction_type(score=score, feedback=feedback)

    return metric


def to_role_dspy_examples(
    records: Iterable[Mapping[str, Any]],
    *,
    role: str,
    dspy_module: Any,
) -> list[Any]:
    """Convert one role's exported records to its exact DSPy signature shape."""
    if role not in ROLE_INPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    examples: list[Any] = []
    for record in records:
        if record.get("role") != role:
            continue
        payload = dict(record["inputs"])
        for field in ROLE_OUTPUT_FIELDS[role]:
            payload[f"expected_{field}"] = record["output"][field]
        payload.update(
            example_id=record["example_id"],
            audit_score=float(record["outcome"]["score"]),
            audit_feedback=str(record["outcome"]["reviewer_feedback"]),
        )
        example = dspy_module.Example(**payload).with_inputs(*ROLE_INPUT_FIELDS[role])
        examples.append(example)
    return examples


def assess_dataset_readiness(
    records: Iterable[Mapping[str, Any]],
    *,
    role: str,
) -> dict[str, Any]:
    """Return deterministic blockers and warnings for one role optimization."""
    if role not in ROLE_INPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    selected = [record for record in records if record.get("role") == role]
    splits = Counter(str(record.get("split")) for record in selected)
    tasks = {str(record.get("task", "")).strip() for record in selected}
    tasks.discard("")
    successful = sum(
        float(record["outcome"].get("score", 0.0)) >= 1.0 for record in selected
    )
    imperfect = len(selected) - successful
    imperfect_train = sum(
        record.get("split") == "train"
        and float(record["outcome"].get("score", 0.0)) < 1.0
        for record in selected
    )
    blockers: list[str] = []
    warnings: list[str] = []
    if splits["train"] < 2:
        blockers.append("role requires at least two training examples")
    if splits["dev"] < 1:
        blockers.append("role requires at least one development example")
    if len(tasks) < 3:
        blockers.append("role requires at least three distinct authoritative tasks")
    if imperfect_train == 0:
        warnings.append("dataset has no imperfect training examples for reflection")
    if splits["holdout"] == 0:
        warnings.append("dataset has no offline holdout examples for later comparison")
    return {
        "role": role,
        "ready": not blockers,
        "counts": {
            "total": len(selected),
            "train": splits["train"],
            "dev": splits["dev"],
            "holdout": splits["holdout"],
            "distinct_tasks": len(tasks),
            "successful": successful,
            "imperfect": imperfect,
        },
        "imperfect_train": imperfect_train,
        "blockers": blockers,
        "warnings": warnings,
    }


def _role_program(role: str) -> Any:
    if role == "explorer":
        from .explorer import ExplorerProgram

        return ExplorerProgram()
    if role == "planner":
        from .planner import PlannerProgram

        return PlannerProgram()
    if role == "implementer":
        from .implementer import ImplementerProgram

        return ImplementerProgram()
    if role == "repairer":
        from .repairer import RepairerProgram

        return RepairerProgram()
    if role == "reviewer":
        from .reviewer import ReviewerProgram

        return ReviewerProgram()
    raise GepaRunnerError(f"Unsupported GEPA role: {role}")


def _score_value(result: Any) -> float:
    if isinstance(result, Mapping):
        return float(result["score"])
    return float(getattr(result, "score"))


def _evaluate_program(
    program: Any,
    examples: list[Any],
    metric: Callable[..., Any],
    *,
    input_fields: tuple[str, ...],
) -> dict[str, Any]:
    scores: list[dict[str, Any]] = []
    for example in examples:
        inputs = {field: getattr(example, field) for field in input_fields}
        prediction = program(**inputs)
        score = _score_value(metric(example, prediction))
        scores.append({"example_id": getattr(example, "example_id"), "score": score})
    return {
        "aggregate_score": mean(item["score"] for item in scores) if scores else 0.0,
        "examples": scores,
    }


def _detailed_result_summary(program: Any) -> dict[str, Any]:
    details = getattr(program, "detailed_results", None)
    if details is None:
        return {}
    scores = [float(value) for value in getattr(details, "val_aggregate_scores", [])]
    best_index = max(range(len(scores)), key=scores.__getitem__) if scores else None
    return {
        "val_aggregate_scores": scores,
        "best_index": best_index,
        "best_score": scores[best_index] if best_index is not None else None,
        "total_metric_calls": getattr(details, "total_metric_calls", None),
        "num_full_val_evals": getattr(details, "num_full_val_evals", None),
        "seed": getattr(details, "seed", None),
    }


def _program_predictor_count(program: Any) -> int:
    predictors = getattr(program, "predictors", None)
    if callable(predictors):
        return max(1, len(list(predictors())))
    named = getattr(program, "named_predictors", None)
    if callable(named):
        return max(1, len(list(named())))
    return 1


def _auto_metric_budget(
    *,
    auto: str,
    predictor_count: int,
    valset_size: int,
    minibatch_size: int = 35,
    full_eval_steps: int = 5,
) -> int:
    candidates = AUTO_CANDIDATES[auto]
    trials = int(
        max(
            2 * (predictor_count * 2) * math.log2(candidates),
            1.5 * candidates,
        )
    )
    total = valset_size + candidates * 5 + trials * minibatch_size
    periodic_fulls = (trials + 1) // full_eval_steps + 1
    extra_final = 1 if trials < full_eval_steps else 0
    return total + (periodic_fulls + extra_final) * valset_size


def _build_lm(
    factory: Callable[..., Any],
    route: str,
    *,
    max_tokens: int | None = None,
) -> Any:
    if max_tokens is None:
        return factory(route)
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    accepts_keyword = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "max_tokens"
        for parameter in parameters
    )
    if accepts_keyword:
        return factory(route, max_tokens=max_tokens)
    return factory(route)


def _program_instructions(program: Any) -> dict[str, str]:
    named = getattr(program, "named_predictors", None)
    if not callable(named):
        return {}
    instructions: dict[str, str] = {}
    for name, predictor in named():
        signature = getattr(predictor, "signature", None)
        instructions[str(name)] = str(getattr(signature, "instructions", ""))
    return instructions


def assess_candidate_instructions(
    baseline: Mapping[str, str],
    candidate: Mapping[str, str],
    *,
    max_instruction_chars: int,
) -> dict[str, Any]:
    """Reject oversized or mechanically repetitive optimized instructions."""
    reasons: list[str] = []
    if not candidate:
        reasons.append("candidate predictor instructions were unavailable")
    for name, text in candidate.items():
        if not text.strip():
            reasons.append(f"{name} instruction is blank")
        if len(text) > max_instruction_chars:
            reasons.append(
                f"{name} instruction has {len(text)} characters; "
                f"limit is {max_instruction_chars}"
            )
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        counts = Counter(lines)
        repeated = sorted(
            line for line, count in counts.items() if count > 2 and len(line) >= 20
        )
        if repeated:
            reasons.append(f"{name} instruction repeats substantive lines")
        normalized_text = text.casefold()
        markers = [
            marker
            for marker in FORBIDDEN_CANDIDATE_MARKERS
            if marker in normalized_text
        ]
        if markers:
            reasons.append(
                f"{name} instruction embeds replay/example scaffolding: "
                + ", ".join(markers)
            )
    return {
        "changed": dict(candidate) != dict(baseline),
        "safe": not reasons,
        "reasons": reasons,
        "baseline_hash": stable_hash(dict(baseline)),
        "candidate_hash": stable_hash(dict(candidate)),
        "max_instruction_chars": max_instruction_chars,
    }


def _compact_feedback(value: Any, *, limit: int = 360) -> str:
    text = str(value or "").replace(REFLECTION_GUARD, "")
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    compact = " ".join(lines)
    return compact[:limit].rstrip()


def _strip_instruction_wrapper(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    prefixes = ("# New Instruction", "## New Instruction", "New instruction:")
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip(" :\n")
    return text


class CompactInstructionProposer:
    """Generate bounded role-level instructions from compact audit feedback."""

    def __init__(
        self,
        dspy_module: Any,
        *,
        role: str,
        max_instruction_chars: int,
        max_unsafe_proposals: int,
    ) -> None:
        self.role = role
        self.max_instruction_chars = max_instruction_chars
        self.max_unsafe_proposals = max_unsafe_proposals
        self.attempted = 0
        self.accepted_by_proposer = 0
        self.rejected_by_proposer = 0
        self.consecutive_unsafe = 0
        self.stop_requested = False
        self.last_rejection_reasons: list[str] = []
        self._predictor = self._build_predictor(dspy_module)

    @staticmethod
    def _build_predictor(dspy_module: Any) -> Any | None:
        required = ("Signature", "InputField", "OutputField", "Predict")
        if not all(hasattr(dspy_module, name) for name in required):
            return None

        class CompactProposalSignature(dspy_module.Signature):
            """Rewrite one reusable instruction from bounded mismatch feedback.

            Return only the role-level instruction. Do not repeat task examples,
            repository evidence, generated outputs, headings, or feedback blocks.
            Do not invent facts. Stay within the supplied character limit.
            """

            current_instruction: str = dspy_module.InputField(
                desc="The current reusable role-level instruction."
            )
            feedback_summary: str = dspy_module.InputField(
                desc="Compact mismatch and reviewer feedback only."
            )
            input_fields: str = dspy_module.InputField(
                desc="Comma-separated typed input field names."
            )
            output_fields: str = dspy_module.InputField(
                desc="Comma-separated typed output field names."
            )
            max_characters: int = dspy_module.InputField(
                desc="Hard maximum length for the new instruction."
            )
            improved_instruction: str = dspy_module.OutputField(
                desc="Only the concise reusable instruction text."
            )

        return dspy_module.Predict(CompactProposalSignature)

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: dict[str, list[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        updated: dict[str, str] = {}
        for component in components_to_update:
            current = str(candidate.get(component, ""))
            examples = reflective_dataset.get(component, [])[:3]
            feedback = [
                f"Example {index}: {_compact_feedback(example.get('Feedback'))}"
                for index, example in enumerate(examples, start=1)
            ]
            feedback_summary = "\n".join(feedback) or "No bounded feedback."
            if self._predictor is None:
                proposed = current
            else:
                result = self._predictor(
                    current_instruction=current,
                    feedback_summary=feedback_summary,
                    input_fields=", ".join(ROLE_INPUT_FIELDS[self.role]),
                    output_fields=", ".join(ROLE_OUTPUT_FIELDS[self.role]),
                    max_characters=self.max_instruction_chars,
                )
                proposed = _strip_instruction_wrapper(
                    _prediction_value(result, "improved_instruction")
                )
            self.attempted += 1
            assessment = assess_candidate_instructions(
                {component: current},
                {component: proposed},
                max_instruction_chars=self.max_instruction_chars,
            )
            if assessment["changed"] and assessment["safe"]:
                self.accepted_by_proposer += 1
                self.consecutive_unsafe = 0
                self.last_rejection_reasons = []
                updated[component] = proposed
                continue
            self.rejected_by_proposer += 1
            self.consecutive_unsafe += 1
            reasons = list(assessment["reasons"])
            if not assessment["changed"]:
                reasons.append("proposal did not change the current instruction")
            self.last_rejection_reasons = reasons
            if self.consecutive_unsafe >= self.max_unsafe_proposals:
                self.stop_requested = True
            updated[component] = current
        return updated

    def summary(self) -> dict[str, Any]:
        return {
            "mode": "compact_typed_proposer",
            "attempted": self.attempted,
            "accepted_by_proposer": self.accepted_by_proposer,
            "rejected_by_proposer": self.rejected_by_proposer,
            "consecutive_unsafe": self.consecutive_unsafe,
            "max_unsafe_proposals": self.max_unsafe_proposals,
            "stop_requested": self.stop_requested,
            "last_rejection_reasons": self.last_rejection_reasons,
        }


def _prepare_output(output: Path) -> None:
    if output.exists():
        raise GepaRunnerError(f"GEPA output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)


def _write_run_directory(
    output: Path,
    *,
    report: dict[str, Any],
    candidate_writer: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    _prepare_output(output)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "run"
        staging.mkdir()
        (staging / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if candidate_writer is not None:
            candidate_writer(staging / "candidate.json")
        files = {
            path.name: _sha256_file(path)
            for path in sorted(staging.iterdir())
            if path.is_file()
        }
        manifest = {
            "schema_version": GEPA_RUN_SCHEMA_VERSION,
            "dataset_hash": report["dataset_hash"],
            "role": report["role"],
            "dry_run": report["dry_run"],
            "files": files,
        }
        manifest["manifest_hash"] = stable_hash(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
    return manifest


def run_gepa_optimization(
    dataset: Path,
    output: Path,
    *,
    role: str,
    dry_run: bool = False,
    reflection_route: str = "local-plan",
    auto: str | None = None,
    target_metric_calls: int | None = None,
    max_metric_calls: int | None = None,
    no_improvement_patience: int | None = DEFAULT_NO_IMPROVEMENT_PATIENCE,
    reflection_max_tokens: int = DEFAULT_REFLECTION_MAX_TOKENS,
    max_instruction_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
    max_unsafe_proposals: int = DEFAULT_MAX_UNSAFE_PROPOSALS,
    hard_model_call_limit: int | None = None,
    allow_perfect_only: bool = False,
    force_search_perfect_baseline: bool = False,
    seed: int = 0,
    num_threads: int = 1,
    dspy_module: Any | None = None,
    lm_factory: Callable[..., Any] = build_dspy_lm,
) -> dict[str, Any]:
    """Validate or run one offline GEPA optimization without activation."""
    if reflection_route not in DSPY_ROUTES:
        raise GepaRunnerError(
            f"Unsupported reflection route: {reflection_route}. "
            f"Choose one of {sorted(DSPY_ROUTES)}."
        )
    if auto is not None and auto not in AUTO_CANDIDATES:
        raise GepaRunnerError("GEPA auto budget must be light, medium, or heavy.")
    explicit_targets = [
        value for value in (target_metric_calls, max_metric_calls) if value is not None
    ]
    if len(explicit_targets) > 1:
        raise GepaRunnerError(
            "Choose either target_metric_calls or the max_metric_calls alias, not both."
        )
    requested_target_metric_calls = explicit_targets[0] if explicit_targets else None
    if auto is not None and requested_target_metric_calls is not None:
        raise GepaRunnerError(
            "Choose either GEPA auto or an explicit target metric-call budget."
        )
    if requested_target_metric_calls is not None and requested_target_metric_calls <= 0:
        raise GepaRunnerError("GEPA target metric calls must be positive.")
    if no_improvement_patience is not None and no_improvement_patience <= 0:
        raise GepaRunnerError("GEPA no-improvement patience must be positive.")
    if reflection_max_tokens <= 0:
        raise GepaRunnerError("GEPA reflection_max_tokens must be positive.")
    if max_instruction_chars <= 0:
        raise GepaRunnerError("GEPA max_instruction_chars must be positive.")
    if max_unsafe_proposals <= 0:
        raise GepaRunnerError("GEPA max_unsafe_proposals must be positive.")
    if hard_model_call_limit is not None and hard_model_call_limit <= 0:
        raise GepaRunnerError("GEPA hard_model_call_limit must be positive.")
    if num_threads <= 0:
        raise GepaRunnerError("GEPA num_threads must be positive.")
    dataset = dataset.resolve()
    output = output.resolve()
    if output == dataset or dataset in output.parents:
        raise GepaRunnerError("GEPA output directory cannot be inside the dataset.")
    try:
        dataset_manifest, records = load_gepa_dataset(dataset)
    except GepaDatasetError as exc:
        raise GepaRunnerError(str(exc)) from exc
    readiness = assess_dataset_readiness(records, role=role)
    if auto is None and requested_target_metric_calls is None:
        requested_target_metric_calls = DEFAULT_MAX_METRIC_CALLS
    report: dict[str, Any] = {
        "schema_version": GEPA_RUN_SCHEMA_VERSION,
        "dataset": str(dataset),
        "dataset_hash": dataset_manifest["dataset_hash"],
        "dataset_schema_version": dataset_manifest.get("schema_version"),
        "role": role,
        "student_route": ROLE_ROUTES.get(role),
        "reflection_route": reflection_route,
        "budget": {
            "auto": auto,
            "requested_target_metric_calls": requested_target_metric_calls,
            "effective_target_metric_calls": None,
            "hard_model_call_limit": hard_model_call_limit,
            "reserved_model_calls": None,
            "no_improvement_patience": no_improvement_patience,
        },
        "reflection": {
            "max_tokens": reflection_max_tokens,
            "guard": REFLECTION_GUARD,
            "proposer": None,
        },
        "candidate_policy": {
            "max_instruction_chars": max_instruction_chars,
            "max_unsafe_proposals": max_unsafe_proposals,
            "allow_perfect_only": allow_perfect_only,
            "force_search_perfect_baseline": force_search_perfect_baseline,
        },
        "seed": seed,
        "num_threads": num_threads,
        "dry_run": dry_run,
        "readiness": readiness,
        "baseline": None,
        "optimization": None,
        "holdout": None,
        "metric_call_accounting": None,
        "model_call_accounting": None,
        "activation": "not_performed",
        "promotion": "not_performed",
    }
    if dry_run:
        manifest = _write_run_directory(output, report=report)
        return {"manifest": manifest, "report": report}
    if not readiness["ready"]:
        joined = "; ".join(readiness["blockers"])
        raise GepaRunnerError(f"Dataset is not ready for role {role}: {joined}")
    if readiness["imperfect_train"] == 0 and not allow_perfect_only:
        raise GepaRunnerError(
            "Dataset has no imperfect training examples. Pass --allow-perfect-only "
            "to run an explicit null-result experiment."
        )
    if dspy_module is None:
        try:
            import dspy as dspy_module
        except ImportError as exc:
            raise GepaRunnerError(
                "DSPy is not installed. Run `make agent-install`."
            ) from exc
    role_records = [record for record in records if record.get("role") == role]
    split_records = {
        split: [record for record in role_records if record.get("split") == split]
        for split in ("train", "dev", "holdout")
    }
    trainset = to_role_dspy_examples(
        split_records["train"], role=role, dspy_module=dspy_module
    )
    devset = to_role_dspy_examples(
        split_records["dev"], role=role, dspy_module=dspy_module
    )
    holdoutset = to_role_dspy_examples(
        split_records["holdout"], role=role, dspy_module=dspy_module
    )
    metric = build_gepa_metric(role, dspy_module=dspy_module)
    student = _role_program(role)
    predictor_count = _program_predictor_count(student)
    model_budget = ModelCallBudget(hard_model_call_limit)
    student_lm_raw = _build_lm(lm_factory, ROLE_ROUTES[role])
    student_lm = BudgetedLM(student_lm_raw, model_budget, "student")
    set_lm = getattr(student, "set_lm", None)
    if callable(set_lm):
        set_lm(student_lm)
    target_metric_calls = requested_target_metric_calls
    if auto is not None:
        target_metric_calls = _auto_metric_budget(
            auto=auto,
            predictor_count=predictor_count,
            valset_size=len(devset),
        )
    assert target_metric_calls is not None
    reflection_reserve = max(
        max_unsafe_proposals,
        no_improvement_patience or 0,
    )
    batch_reserve = len(trainset) + len(devset)
    reserved_model_calls = (
        len(devset) * predictor_count
        + len(holdoutset) * predictor_count * 2
        + reflection_reserve
        + batch_reserve * predictor_count
    )
    if hard_model_call_limit is not None:
        available_model_calls = hard_model_call_limit - reserved_model_calls
        if available_model_calls <= 0:
            raise GepaRunnerError(
                "Hard model-call limit is too small for baseline, reflection, "
                "paired holdout, and one GEPA batch reserve."
            )
        available_metric_calls = max(1, available_model_calls // predictor_count)
        target_metric_calls = min(target_metric_calls, available_metric_calls)
    report["budget"]["effective_target_metric_calls"] = target_metric_calls
    report["budget"]["reserved_model_calls"] = reserved_model_calls
    proposer = CompactInstructionProposer(
        dspy_module,
        role=role,
        max_instruction_chars=max_instruction_chars,
        max_unsafe_proposals=max_unsafe_proposals,
    )
    stopper = BoundedNoImprovementStopper(
        target_metric_calls,
        no_improvement_patience,
        proposal_guard=proposer,
        model_budget=model_budget,
    )
    baseline_instructions = _program_instructions(student)
    adapter = dspy_module.JSONAdapter()
    try:
        with dspy_module.context(adapter=adapter, track_usage=True):
            baseline = _evaluate_program(
                student,
                devset,
                metric,
                input_fields=ROLE_INPUT_FIELDS[role],
            )
    except ModelCallBudgetExceeded as exc:
        raise GepaRunnerError(str(exc)) from exc
    perfect_baseline = baseline["aggregate_score"] >= 1.0
    search_performed = not perfect_baseline or force_search_perfect_baseline
    hard_limit_reached = False
    if search_performed:
        reflection_lm_raw = _build_lm(
            lm_factory,
            reflection_route,
            max_tokens=reflection_max_tokens,
        )
        reflection_lm = BudgetedLM(
            reflection_lm_raw,
            model_budget,
            "reflection",
        )
        try:
            with tempfile.TemporaryDirectory(prefix="local-coder-gepa-log-") as log_dir:
                with dspy_module.context(adapter=adapter, track_usage=True):
                    optimizer = dspy_module.GEPA(
                        metric=metric,
                        reflection_lm=reflection_lm,
                        instruction_proposer=proposer,
                        max_metric_calls=target_metric_calls,
                        num_threads=num_threads,
                        track_stats=True,
                        log_dir=log_dir,
                        seed=seed,
                        gepa_kwargs={"stop_callbacks": stopper},
                    )
                    optimized = optimizer.compile(
                        student,
                        trainset=trainset,
                        valset=devset,
                    )
            details = _detailed_result_summary(optimized)
        except ModelCallBudgetExceeded:
            hard_limit_reached = True
            stopper.reason = "hard_model_call_limit"
            optimized = student
            details = {
                "val_aggregate_scores": [baseline["aggregate_score"]],
                "best_index": 0,
                "best_score": baseline["aggregate_score"],
                "total_metric_calls": max(
                    0,
                    model_budget.counts["student"] - len(devset),
                ),
                "num_full_val_evals": 0,
                "seed": seed,
            }
    else:
        stopper.reason = "perfect_baseline"
        optimized = student
        details = {
            "val_aggregate_scores": [baseline["aggregate_score"]],
            "best_index": 0,
            "best_score": baseline["aggregate_score"],
            "total_metric_calls": 0,
            "num_full_val_evals": 0,
            "seed": seed,
        }
    if model_budget.blocked_calls:
        hard_limit_reached = True
        stopper.reason = "hard_model_call_limit"
    proposed_instructions = _program_instructions(optimized)
    safety = assess_candidate_instructions(
        baseline_instructions,
        proposed_instructions,
        max_instruction_chars=max_instruction_chars,
    )
    best_score = details.get("best_score")
    improvement = (
        float(best_score) - float(baseline["aggregate_score"])
        if best_score is not None
        else 0.0
    )
    accept_candidate = bool(
        not hard_limit_reached
        and safety["changed"]
        and safety["safe"]
        and improvement > 0
    )
    selected = optimized if accept_candidate else student
    selected_instructions = _program_instructions(selected)
    selected_changed = selected_instructions != baseline_instructions
    proposer_summary = proposer.summary()
    if not search_performed:
        outcome = "baseline_perfect_no_search"
    elif hard_limit_reached:
        outcome = "hard_model_call_limit"
    elif improvement > 0 and not safety["safe"]:
        outcome = "rejected_unsafe_candidate"
    elif proposer_summary["rejected_by_proposer"] and improvement <= 0:
        outcome = "rejected_reflection_candidates"
    elif accept_candidate:
        outcome = "improved_candidate"
    else:
        outcome = "no_improvement"
    rejection_reasons = list(safety["reasons"])
    if outcome == "rejected_reflection_candidates":
        rejection_reasons.extend(proposer_summary["last_rejection_reasons"])
    if hard_limit_reached:
        rejection_reasons.append(
            "hard model-call limit blocked one or more required calls"
        )
    details.update(
        {
            "baseline_score": baseline["aggregate_score"],
            "improvement": improvement,
            "winning_candidate": "optimized" if accept_candidate else "baseline",
            "proposed_candidate_changed": safety["changed"],
            "selected_candidate_changed": selected_changed,
            "candidate_changed": selected_changed,
            "candidate_safe": safety["safe"],
            "candidate_accepted": accept_candidate,
            "candidate_rejection_reasons": sorted(set(rejection_reasons)),
            "optimization_outcome": outcome,
            "search_performed": search_performed,
            "perfect_baseline": perfect_baseline,
            "instruction_hashes": {
                "baseline": safety["baseline_hash"],
                "proposed": safety["candidate_hash"],
                "selected": stable_hash(selected_instructions),
            },
            "early_stopping": stopper.summary(),
        }
    )
    holdout: dict[str, Any]
    if holdoutset:
        try:
            with dspy_module.context(adapter=adapter, track_usage=True):
                baseline_holdout = _evaluate_program(
                    student,
                    holdoutset,
                    metric,
                    input_fields=ROLE_INPUT_FIELDS[role],
                )
                if selected is student:
                    selected_holdout = baseline_holdout
                    metric_calls = len(holdoutset)
                else:
                    selected_holdout = _evaluate_program(
                        selected,
                        holdoutset,
                        metric,
                        input_fields=ROLE_INPUT_FIELDS[role],
                    )
                    metric_calls = len(holdoutset) * 2
            holdout = {
                "evaluated_after_optimization": True,
                "exposed_during_optimization": False,
                "count": len(holdoutset),
                "baseline": baseline_holdout,
                "selected_candidate": selected_holdout,
                "delta": (
                    selected_holdout["aggregate_score"]
                    - baseline_holdout["aggregate_score"]
                ),
                "metric_calls": metric_calls,
            }
        except ModelCallBudgetExceeded:
            hard_limit_reached = True
            accept_candidate = False
            selected = student
            selected_instructions = baseline_instructions
            selected_changed = False
            outcome = "hard_model_call_limit"
            stopper.reason = outcome
            reasons = set(details.get("candidate_rejection_reasons", []))
            reasons.add("hard model-call limit blocked holdout evaluation")
            details.update(
                {
                    "winning_candidate": "baseline",
                    "selected_candidate_changed": False,
                    "candidate_changed": False,
                    "candidate_accepted": False,
                    "candidate_rejection_reasons": sorted(reasons),
                    "optimization_outcome": outcome,
                    "instruction_hashes": {
                        **details["instruction_hashes"],
                        "selected": stable_hash(baseline_instructions),
                    },
                    "early_stopping": stopper.summary(),
                }
            )
            holdout = {
                "evaluated_after_optimization": False,
                "exposed_during_optimization": False,
                "count": len(holdoutset),
                "reason": "hard model-call limit reached before holdout completed",
                "metric_calls": 0,
            }
    else:
        holdout = {
            "evaluated_after_optimization": False,
            "exposed_during_optimization": False,
            "count": 0,
            "reason": "dataset has no holdout examples for the selected role",
            "metric_calls": 0,
        }
    report["baseline"] = baseline
    report["optimization"] = details
    report["holdout"] = holdout
    report["reflection"]["proposer"] = proposer.summary()
    gepa_metric_calls = int(details.get("total_metric_calls") or 0)
    observed_metric_calls = (
        len(devset) + gepa_metric_calls + int(holdout["metric_calls"])
    )
    report["metric_call_accounting"] = {
        "baseline_dev": len(devset),
        "gepa_reported_metric_calls": gepa_metric_calls,
        "post_optimization_holdout": holdout["metric_calls"],
        "total_observed": observed_metric_calls,
        "target_metric_calls": target_metric_calls,
        "target_overrun": max(0, gepa_metric_calls - target_metric_calls),
    }
    report["model_call_accounting"] = model_budget.summary()

    def write_candidate(path: Path) -> None:
        selected_set_lm = getattr(selected, "set_lm", None)
        if callable(selected_set_lm):
            selected_set_lm(student_lm_raw)
        selected.save(str(path))

    manifest = _write_run_directory(
        output,
        report=report,
        candidate_writer=write_candidate,
    )
    return {"manifest": manifest, "report": report}
