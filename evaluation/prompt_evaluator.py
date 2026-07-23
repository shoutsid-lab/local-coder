"""Trusted paired evaluation for inert prompt-candidate program states."""

from __future__ import annotations

import difflib
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping

from runtime.state import StateStore

from .outcomes import hash_text, stable_hash
from .scorecard import Gate, PromotionScorecard
from .supervisor import EvaluationBudget, environment_identity

PROMPT_HOLDOUT_SCHEMA_VERSION = 1
PROMPT_HOLDOUT_SUITE_KIND = "prompt-replay"
PROMPT_EVALUATION_SCHEMA_VERSION = 1


class PromptEvaluationError(ValueError):
    """Raised when paired prompt evaluation cannot proceed safely."""


@dataclass(frozen=True)
class PromptHoldoutCase:
    """One external prompt replay input without its oracle output."""

    case_id: str
    inputs: dict[str, Any]


@dataclass(frozen=True)
class PromptHoldoutSuite:
    """Validated external prompt holdout manifest."""

    suite_id: str
    role: str
    cases: tuple[PromptHoldoutCase, ...]
    manifest_hash: str
    path: Path


@dataclass(frozen=True)
class PromptCaseResult:
    """One baseline or candidate prompt replay result."""

    generation: str
    repetition: int
    case_id: str
    visibility: str
    score: float
    schema_valid: bool
    duration_ms: float
    observation_hash: str | None
    failure: str | None

    def to_dict(self, *, redact_holdout: bool = True) -> dict[str, Any]:
        """Serialize without revealing holdout observations or exact case scores."""
        redacted = redact_holdout and self.visibility == "holdout"
        return {
            "generation": self.generation,
            "repetition": self.repetition,
            "case_id": self.case_id,
            "visibility": self.visibility,
            "score": "<redacted>" if redacted else self.score,
            "schema_valid": self.schema_valid,
            "process": {
                "command": ["trusted-prompt-replay"],
                "returncode": 0 if self.failure is None else 1,
                "timed_out": False,
                "duration_ms": self.duration_ms,
                "stdout": "<redacted holdout output>" if redacted else "",
                "stderr": "",
                "output_truncated": False,
            },
            "observation_hash": ("<redacted>" if redacted else self.observation_hash),
            "oracle_passed": self.schema_valid,
            "policy_passed": self.failure is None,
            "failure": self.failure,
        }


@dataclass(frozen=True)
class PromptPairedEvaluation:
    """Complete prompt replay evidence under one frozen campaign identity."""

    baseline_commit: str
    candidate_instruction_hash: str
    dataset_hash: str
    dataset_manifest_hash: str
    holdout_suite_hash: str
    holdout_oracle_hash: str
    environment_hash: str
    evaluator_hash: str
    repetitions: int
    budget: EvaluationBudget
    model_call_accounting: dict[str, Any]
    results: tuple[PromptCaseResult, ...]
    evaluation_id: str | None = None
    build_id: str | None = None

    def aggregate(self, generation: str, visibility: str) -> float:
        """Return the mean score for one generation and visibility."""
        scores = [
            result.score
            for result in self.results
            if result.generation == generation and result.visibility == visibility
        ]
        return mean(scores) if scores else 0.0

    def to_dict(self, *, redact_holdout: bool = True) -> dict[str, Any]:
        """Return an inspection-ready report with holdout observations redacted."""
        return {
            "schema_version": PROMPT_EVALUATION_SCHEMA_VERSION,
            "artifact_kind": "prompt_evaluation",
            "baseline_commit": self.baseline_commit,
            "candidate_instruction_hash": self.candidate_instruction_hash,
            "dataset_hash": self.dataset_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "holdout_suite_hash": self.holdout_suite_hash,
            "holdout_oracle_hash": self.holdout_oracle_hash,
            "environment_hash": self.environment_hash,
            "evaluator_hash": self.evaluator_hash,
            "repetitions": self.repetitions,
            "budget": asdict(self.budget),
            "model_call_accounting": self.model_call_accounting,
            "evaluation_id": self.evaluation_id,
            "build_id": self.build_id,
            "development": {
                "baseline_score": self.aggregate("baseline", "development"),
                "candidate_score": self.aggregate("candidate", "development"),
                "delta": (
                    self.aggregate("candidate", "development")
                    - self.aggregate("baseline", "development")
                ),
            },
            "holdout": {
                "baseline_score": self.aggregate("baseline", "holdout"),
                "candidate_score": self.aggregate("candidate", "holdout"),
                "delta": (
                    self.aggregate("candidate", "holdout")
                    - self.aggregate("baseline", "holdout")
                ),
                "oracle_outputs": "redacted",
            },
            "activation": "not_performed",
            "promotion": "not_performed",
            "results": [
                result.to_dict(redact_holdout=redact_holdout) for result in self.results
            ],
        }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptEvaluationError(
            f"Could not load trusted prompt JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise PromptEvaluationError("Trusted prompt JSON must be an object.")
    return payload


def _role_fields(role: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from runtime.dspy_programs.gepa_runner import (
        ROLE_INPUT_FIELDS,
        ROLE_OUTPUT_FIELDS,
    )

    if role not in ROLE_INPUT_FIELDS or role not in ROLE_OUTPUT_FIELDS:
        raise PromptEvaluationError(f"Unsupported prompt evaluation role: {role}")
    return ROLE_INPUT_FIELDS[role], ROLE_OUTPUT_FIELDS[role]


def load_prompt_holdout(
    manifest_path: Path,
    oracle_path: Path,
    *,
    expected_role: str | None = None,
) -> tuple[PromptHoldoutSuite, dict[str, dict[str, Any]], str]:
    """Load a prompt holdout manifest and separate base-owned oracle."""
    manifest_path = manifest_path.resolve()
    oracle_path = oracle_path.resolve()
    manifest = _read_object(manifest_path)
    expected_manifest_fields = {
        "schema_version",
        "suite_kind",
        "suite_id",
        "visibility",
        "role",
        "cases",
    }
    if set(manifest) != expected_manifest_fields:
        raise PromptEvaluationError("Prompt holdout manifest has unsupported fields.")
    if manifest["schema_version"] != PROMPT_HOLDOUT_SCHEMA_VERSION:
        raise PromptEvaluationError("Prompt holdout manifest schema mismatch.")
    if manifest["suite_kind"] != PROMPT_HOLDOUT_SUITE_KIND:
        raise PromptEvaluationError("Holdout is not a prompt-replay suite.")
    if manifest["visibility"] != "holdout":
        raise PromptEvaluationError("Prompt holdout visibility must be holdout.")
    suite_id = manifest["suite_id"]
    role = manifest["role"]
    if not isinstance(suite_id, str) or not suite_id:
        raise PromptEvaluationError("Prompt holdout suite ID must be non-empty text.")
    if not isinstance(role, str) or not role:
        raise PromptEvaluationError("Prompt holdout role must be non-empty text.")
    if expected_role is not None and role != expected_role:
        raise PromptEvaluationError("Prompt holdout role differs from the campaign.")
    input_fields, output_fields = _role_fields(role)
    raw_cases = manifest["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PromptEvaluationError("Prompt holdout requires at least one case.")
    cases: list[PromptHoldoutCase] = []
    case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict) or set(raw_case) != {"id", "inputs"}:
            raise PromptEvaluationError("Prompt holdout case has unsupported fields.")
        case_id = raw_case["id"]
        inputs = raw_case["inputs"]
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise PromptEvaluationError("Prompt holdout case IDs must be unique text.")
        if not isinstance(inputs, dict) or set(inputs) != set(input_fields):
            raise PromptEvaluationError(
                f"Prompt holdout inputs must match the {role} signature."
            )
        cases.append(PromptHoldoutCase(case_id=case_id, inputs=dict(inputs)))
        case_ids.add(case_id)

    oracle_payload = _read_object(oracle_path)
    expected_oracle_fields = {
        "schema_version",
        "suite_kind",
        "suite_id",
        "role",
        "cases",
    }
    if set(oracle_payload) != expected_oracle_fields:
        raise PromptEvaluationError("Prompt holdout oracle has unsupported fields.")
    if oracle_payload["schema_version"] != PROMPT_HOLDOUT_SCHEMA_VERSION:
        raise PromptEvaluationError("Prompt holdout oracle schema mismatch.")
    if oracle_payload["suite_kind"] != PROMPT_HOLDOUT_SUITE_KIND:
        raise PromptEvaluationError("Prompt oracle is not a prompt-replay oracle.")
    if oracle_payload["suite_id"] != suite_id or oracle_payload["role"] != role:
        raise PromptEvaluationError("Prompt holdout manifest and oracle differ.")
    raw_oracle = oracle_payload["cases"]
    if not isinstance(raw_oracle, dict) or set(raw_oracle) != case_ids:
        raise PromptEvaluationError("Prompt holdout oracle case IDs differ.")
    oracle: dict[str, dict[str, Any]] = {}
    for case_id, value in raw_oracle.items():
        if not isinstance(value, dict) or set(value) != {"output"}:
            raise PromptEvaluationError("Prompt holdout oracle case is malformed.")
        output = value["output"]
        if not isinstance(output, dict) or set(output) != set(output_fields):
            raise PromptEvaluationError(
                f"Prompt holdout output must match the {role} signature."
            )
        oracle[case_id] = dict(output)
    return (
        PromptHoldoutSuite(
            suite_id=suite_id,
            role=role,
            cases=tuple(cases),
            manifest_hash=stable_hash(manifest),
            path=manifest_path,
        ),
        oracle,
        stable_hash(oracle_payload),
    )


def prompt_holdout_identity(
    suite: PromptHoldoutSuite,
    oracle_hash: str,
) -> str:
    """Return the immutable manifest-plus-oracle identity."""
    return stable_hash({"manifest": suite.manifest_hash, "oracle": oracle_hash})


def prompt_evaluator_identity(
    trusted_root: Path,
    role: str,
) -> tuple[dict[str, Any], str]:
    """Hash the trusted prompt evaluator, role program, and LM configuration."""
    relative_paths = (
        "evaluation/prompt_evaluator.py",
        "runtime/dspy_lm.py",
        "runtime/dspy_programs/gepa_runner.py",
        f"runtime/dspy_programs/{role}.py",
        "litellm-config.yaml",
        "requirements-agent.txt",
    )
    files: dict[str, str | None] = {}
    for relative in relative_paths:
        path = trusted_root / relative
        files[relative] = (
            hash_text(path.read_text(encoding="utf-8")) if path.is_file() else None
        )
    identity = {"role": role, "configuration": files}
    return identity, stable_hash(identity)


def _field_similarity(expected: Any, actual: Any) -> float:
    if expected == actual:
        return 1.0
    if type(expected) is not type(actual):
        return 0.0
    if isinstance(expected, (str, list, dict)):
        expected_text = json.dumps(expected, sort_keys=True, ensure_ascii=True)
        actual_text = json.dumps(actual, sort_keys=True, ensure_ascii=True)
        return difflib.SequenceMatcher(None, expected_text, actual_text).ratio()
    return 0.0


def score_prompt_output(
    role: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    audit_score: float = 1.0,
) -> tuple[float, bool]:
    """Score one typed role output with the same structural replay semantics."""
    _, output_fields = _role_fields(role)
    schema_valid = set(actual) == set(output_fields) and all(
        type(actual[field]) is type(expected[field]) for field in output_fields
    )
    similarities = [
        _field_similarity(expected[field], actual.get(field)) for field in output_fields
    ]
    structural = mean(similarities) if similarities else 0.0
    return structural * (0.5 + 0.5 * audit_score), schema_valid


def _candidate_artifact(build: dict[str, Any]) -> dict[str, Any]:
    from evaluation.prompt_campaign import load_prompt_candidate_artifact

    return load_prompt_candidate_artifact(build)


def _validate_candidate_identity(
    artifact: dict[str, Any],
    spec: Any,
) -> None:
    """Require the stored candidate to match the approved prompt brief."""
    expected = {
        "schema_version": 2,
        "artifact_kind": "prompt_candidate",
        "campaign_kind": "prompt-optimization",
        "role": spec.role,
        "dataset": spec.dataset,
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
    }
    mismatches = [
        name for name, value in expected.items() if artifact.get(name) != value
    ]
    if mismatches:
        raise PromptEvaluationError(
            "Prompt candidate identity differs from the approved brief: "
            f"{sorted(mismatches)}"
        )


def _candidate_state(
    artifact: dict[str, Any],
) -> tuple[Path, str, str]:
    output = artifact.get("output")
    expected_hash = artifact.get("gepa_candidate_hash")
    if not isinstance(output, str) or not output:
        raise PromptEvaluationError("Prompt candidate output path is missing.")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise PromptEvaluationError("Prompt candidate state hash is missing.")
    path = Path(output).resolve() / "candidate.json"
    if not path.is_file():
        raise PromptEvaluationError("Prompt candidate state file is missing.")
    content = path.read_text(encoding="utf-8")
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise PromptEvaluationError("Prompt candidate state hash changed after build.")
    return path, content, actual_hash


def _development_cases(
    spec: Any,
) -> list[tuple[str, dict[str, Any], dict[str, Any], float]]:
    from runtime.dspy_programs.gepa_dataset import load_gepa_dataset

    manifest, records = load_gepa_dataset(Path(spec.dataset))
    if (
        manifest.get("dataset_hash") != spec.dataset_hash
        or manifest.get("manifest_hash") != spec.dataset_manifest_hash
    ):
        raise PromptEvaluationError(
            "Prompt dataset identity changed before evaluation."
        )
    input_fields, output_fields = _role_fields(spec.role)
    selected: list[tuple[str, dict[str, Any], dict[str, Any], float]] = []
    case_ids: set[str] = set()
    for record in records:
        if record.get("role") != spec.role or record.get("split") != "dev":
            continue
        case_id = record.get("example_id")
        inputs = record.get("inputs")
        output = record.get("output")
        outcome = record.get("outcome")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise PromptEvaluationError(
                "Prompt development case IDs must be unique text."
            )
        if not isinstance(inputs, dict) or set(inputs) != set(input_fields):
            raise PromptEvaluationError(
                f"Prompt development inputs must match the {spec.role} signature."
            )
        if not isinstance(output, dict) or set(output) != set(output_fields):
            raise PromptEvaluationError(
                f"Prompt development output must match the {spec.role} signature."
            )
        score = outcome.get("score") if isinstance(outcome, dict) else None
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0.0 <= float(score) <= 1.0
        ):
            raise PromptEvaluationError(
                "Prompt development audit score must be between zero and one."
            )
        selected.append((case_id, dict(inputs), dict(output), float(score)))
        case_ids.add(case_id)
    if not selected:
        raise PromptEvaluationError(
            "Prompt evaluation dataset has no development cases."
        )
    return selected


def _runtime_prediction_runner(
    *,
    role: str,
    candidate_path: Path,
    budget: EvaluationBudget,
) -> tuple[
    Callable[[str, dict[str, Any]], Mapping[str, Any]],
    Callable[[], dict[str, Any]],
]:
    try:
        import dspy
    except ImportError as exc:
        raise PromptEvaluationError(
            "DSPy is not installed. Run `make agent-install`."
        ) from exc
    from runtime.dspy_programs.gepa_runner import (
        BudgetedLM,
        ModelCallBudget,
        ROLE_OUTPUT_FIELDS,
        ROLE_ROUTES,
        _role_program,
    )
    from runtime.dspy_lm import build_dspy_lm

    model_budget = ModelCallBudget(budget.max_model_calls)
    token_totals = {"prompt": 0, "completion": 0}
    route = ROLE_ROUTES[role]
    programs = {"baseline": _role_program(role), "candidate": _role_program(role)}
    load = getattr(programs["candidate"], "load", None)
    if not callable(load):
        raise PromptEvaluationError("DSPy role program cannot load candidate state.")
    load(str(candidate_path))
    for generation, program in programs.items():
        lm_raw = build_dspy_lm(
            route,
            max_tokens=max(1, min(2048, budget.max_completion_tokens)),
            timeout=budget.process_wall_seconds,
        )
        lm = BudgetedLM.wrap(
            lm_raw,
            model_budget,
            generation,
            base_lm_type=getattr(dspy, "BaseLM", None),
        )
        set_lm = getattr(program, "set_lm", None)
        if not callable(set_lm):
            raise PromptEvaluationError("DSPy role program cannot bind an LM.")
        set_lm(lm)
    adapter = dspy.JSONAdapter()

    def predict(generation: str, inputs: dict[str, Any]) -> Mapping[str, Any]:
        program = programs[generation]
        with dspy.context(adapter=adapter, track_usage=True):
            prediction = program(**inputs)
        get_usage = getattr(prediction, "get_lm_usage", None)
        usage_by_lm = get_usage() if callable(get_usage) else None
        usage = (
            usage_by_lm.get(f"openai/{route}")
            if isinstance(usage_by_lm, Mapping)
            else None
        )
        if not isinstance(usage, Mapping) and isinstance(usage_by_lm, Mapping):
            usage = next(
                (item for item in usage_by_lm.values() if isinstance(item, Mapping)),
                None,
            )
        prompt_tokens = (
            usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        )
        completion_tokens = (
            usage.get("completion_tokens") if isinstance(usage, Mapping) else None
        )
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
        ):
            raise PromptEvaluationError(
                "Prompt evaluation model usage was not reported."
            )
        token_totals["prompt"] += prompt_tokens
        token_totals["completion"] += completion_tokens
        if token_totals["prompt"] > budget.max_prompt_tokens:
            raise PromptEvaluationError(
                "Prompt evaluation prompt-token budget exhausted."
            )
        if token_totals["completion"] > budget.max_completion_tokens:
            raise PromptEvaluationError(
                "Prompt evaluation completion-token budget exhausted."
            )
        output: dict[str, Any] = {}
        for field in ROLE_OUTPUT_FIELDS[role]:
            if isinstance(prediction, Mapping):
                output[field] = prediction.get(field)
            else:
                output[field] = getattr(prediction, field, None)
        return output

    def accounting() -> dict[str, Any]:
        return {
            "hard_limit": model_budget.limit,
            "baseline": model_budget.counts["baseline"],
            "candidate": model_budget.counts["candidate"],
            "total": model_budget.total,
            "at_limit": model_budget.at_limit,
            "blocked_calls": model_budget.blocked_calls,
            "prompt_tokens": token_totals["prompt"],
            "completion_tokens": token_totals["completion"],
            "prompt_token_limit": budget.max_prompt_tokens,
            "completion_token_limit": budget.max_completion_tokens,
            "provider_retries_included": False,
        }

    return predict, accounting


def _instruction_hashes(role: str, candidate_path: Path) -> tuple[str, str]:
    from runtime.dspy_programs.gepa_runner import _program_instructions, _role_program

    baseline = _role_program(role)
    candidate = _role_program(role)
    load = getattr(candidate, "load", None)
    if not callable(load):
        raise PromptEvaluationError("DSPy role program cannot load candidate state.")
    load(str(candidate_path))
    return stable_hash(_program_instructions(baseline)), stable_hash(
        _program_instructions(candidate)
    )


def _accounting_failures(
    accounting: Mapping[str, Any],
    *,
    expected_limit: int,
    expected_prompt_tokens: int,
    expected_completion_tokens: int,
) -> list[str]:
    """Validate hard-call accounting before it can satisfy a control gate."""
    failures: list[str] = []
    integer_fields = ("hard_limit", "baseline", "candidate", "total", "blocked_calls")
    values: dict[str, int] = {}
    for name in integer_fields:
        value = accounting.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(f"invalid_{name}")
        else:
            values[name] = value
    if values.get("hard_limit") != expected_limit:
        failures.append("hard_limit_mismatch")
    if (
        "baseline" in values
        and "candidate" in values
        and "total" in values
        and values["baseline"] + values["candidate"] != values["total"]
    ):
        failures.append("model_call_total_mismatch")
    if values.get("total", expected_limit + 1) > expected_limit:
        failures.append("model_call_limit_exceeded")
    if values.get("blocked_calls") != 0:
        failures.append("evaluation_model_call_blocked")
    token_limits = {
        "prompt_tokens": expected_prompt_tokens,
        "completion_tokens": expected_completion_tokens,
    }
    for name, limit in token_limits.items():
        value = accounting.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(f"invalid_{name}")
        elif value > limit:
            failures.append(f"{name}_limit_exceeded")
    if accounting.get("prompt_token_limit") != expected_prompt_tokens:
        failures.append("prompt_token_limit_mismatch")
    if accounting.get("completion_token_limit") != expected_completion_tokens:
        failures.append("completion_token_limit_mismatch")
    at_limit = accounting.get("at_limit")
    if not isinstance(at_limit, bool):
        failures.append("invalid_at_limit")
    elif "total" in values and "hard_limit" in values:
        if at_limit is not (values["total"] >= values["hard_limit"]):
            failures.append("at_limit_mismatch")
    if accounting.get("provider_retries_included") is not False:
        failures.append("provider_retry_scope_unknown")
    return failures


def _build_accounting_failures(artifact: Mapping[str, Any]) -> list[str]:
    """Validate the candidate-build model-call ledger frozen in the artifact."""
    budget = artifact.get("budget")
    accounting = artifact.get("model_call_accounting")
    if not isinstance(budget, Mapping):
        return ["missing_candidate_build_budget"]
    expected_limit = budget.get("hard_model_call_limit")
    if (
        isinstance(expected_limit, bool)
        or not isinstance(expected_limit, int)
        or expected_limit <= 0
    ):
        return ["invalid_candidate_build_hard_limit"]
    if not isinstance(accounting, Mapping):
        return ["missing_candidate_build_model_call_accounting"]
    failures: list[str] = []
    names = ("hard_limit", "student", "reflection", "total", "blocked_calls")
    values: dict[str, int] = {}
    for name in names:
        value = accounting.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(f"invalid_candidate_build_{name}")
        else:
            values[name] = value
    if values.get("hard_limit") != expected_limit:
        failures.append("candidate_build_hard_limit_mismatch")
    if (
        "student" in values
        and "reflection" in values
        and "total" in values
        and values["student"] + values["reflection"] != values["total"]
    ):
        failures.append("candidate_build_model_call_total_mismatch")
    if values.get("total", expected_limit + 1) > expected_limit:
        failures.append("candidate_build_model_call_limit_exceeded")
    if values.get("blocked_calls") != 0:
        failures.append("candidate_build_model_call_blocked")
    at_limit = accounting.get("at_limit")
    if not isinstance(at_limit, bool):
        failures.append("invalid_candidate_build_at_limit")
    elif "total" in values and "hard_limit" in values:
        if at_limit is not (values["total"] >= values["hard_limit"]):
            failures.append("candidate_build_at_limit_mismatch")
    if accounting.get("provider_retries_included") is not False:
        failures.append("candidate_build_provider_retry_scope_unknown")
    return failures


def build_prompt_scorecard(
    evaluation: PromptPairedEvaluation,
    *,
    artifact: dict[str, Any],
) -> PromotionScorecard:
    """Apply standard gate names to prompt-specific paired evidence."""
    candidate_results = [
        result for result in evaluation.results if result.generation == "candidate"
    ]
    holdout_candidate = [
        result for result in candidate_results if result.visibility == "holdout"
    ]
    baseline_by_key = {
        (result.repetition, result.case_id, result.visibility): result
        for result in evaluation.results
        if result.generation == "baseline"
    }
    safety_passed = bool(
        artifact.get("candidate_accepted") is True
        and artifact.get("selected_candidate_changed") is True
        and artifact.get("winning_candidate") == "optimized"
        and artifact.get("activation") == "not_performed"
        and artifact.get("promotion") == "not_performed"
    )
    safety = Gate(
        "safety",
        safety_passed,
        {
            "candidate_accepted": artifact.get("candidate_accepted"),
            "candidate_changed": artifact.get("selected_candidate_changed"),
            "activation": artifact.get("activation"),
            "promotion": artifact.get("promotion"),
        },
    )
    correctness_failures = [
        result.case_id
        for result in candidate_results
        if not result.schema_valid or result.failure is not None
    ]
    correctness = Gate(
        "correctness",
        not correctness_failures,
        {"failures": correctness_failures},
    )
    regressions = []
    for result in holdout_candidate:
        baseline = baseline_by_key.get(
            (result.repetition, result.case_id, result.visibility)
        )
        if baseline is None or result.score + 1e-12 < baseline.score:
            regressions.append(result.case_id)
    holdout_delta = evaluation.aggregate("candidate", "holdout") - evaluation.aggregate(
        "baseline", "holdout"
    )
    regression = Gate(
        "regression",
        not regressions and holdout_delta >= -1e-12,
        {
            "worse_cases": sorted(set(regressions)),
            "aggregate_delta": holdout_delta,
        },
    )
    accounting = evaluation.model_call_accounting
    build_accounting = artifact.get("model_call_accounting")
    control_failures = _accounting_failures(
        accounting,
        expected_limit=evaluation.budget.max_model_calls,
        expected_prompt_tokens=evaluation.budget.max_prompt_tokens,
        expected_completion_tokens=evaluation.budget.max_completion_tokens,
    )
    control_failures.extend(_build_accounting_failures(artifact))
    control = Gate(
        "control",
        not control_failures,
        {
            "failures": control_failures,
            "evaluation_model_calls": accounting,
            "candidate_build_model_calls": build_accounting,
            "evaluator_hash": evaluation.evaluator_hash,
        },
    )
    development_delta = evaluation.aggregate(
        "candidate", "development"
    ) - evaluation.aggregate("baseline", "development")
    improvement = Gate(
        "improvement",
        development_delta > 1e-12,
        {
            "baseline_score": evaluation.aggregate("baseline", "development"),
            "candidate_score": evaluation.aggregate("candidate", "development"),
            "delta": development_delta,
        },
    )
    total_wall_ms = sum(result.duration_ms for result in evaluation.results)
    efficiency_passed = bool(
        total_wall_ms <= evaluation.budget.campaign_wall_seconds * 1000
        and accounting.get("total", evaluation.budget.max_model_calls + 1)
        <= evaluation.budget.max_model_calls
    )
    efficiency = Gate(
        "efficiency",
        efficiency_passed,
        {
            "evaluation_wall_time_ms": total_wall_ms,
            "model_calls": accounting.get("total"),
            "model_call_limit": evaluation.budget.max_model_calls,
        },
    )
    gates = (safety, correctness, regression, control, improvement, efficiency)
    failed = next((gate.name for gate in gates if gate.passed is not True), None)
    recommendation = f"reject_at_{failed}" if failed else "eligible_for_promotion"
    return PromotionScorecard(gates=gates, recommendation=recommendation)


def evaluate_prompt_pair(
    *,
    trusted_root: Path,
    campaign: dict[str, Any],
    build: dict[str, Any],
    spec: Any,
    holdout: PromptHoldoutSuite,
    holdout_oracle: dict[str, dict[str, Any]],
    holdout_oracle_hash: str,
    repetitions: int,
    budget: EvaluationBudget,
    state: StateStore,
    campaign_id: str,
    build_id: str,
    expected_environment_hash: str | None = None,
    prediction_runner: Callable[[str, dict[str, Any]], Mapping[str, Any]] | None = None,
    accounting: Callable[[], dict[str, Any]] | None = None,
    instruction_hash_loader: Callable[[str, Path], tuple[str, str]] | None = None,
) -> tuple[PromptPairedEvaluation, PromotionScorecard]:
    """Evaluate baseline and candidate prompt states on dev and external holdout."""
    if repetitions < 1 or repetitions > 10:
        raise PromptEvaluationError("Repetitions must be between 1 and 10.")
    budget.validate()
    artifact = _candidate_artifact(build)
    _validate_candidate_identity(artifact, spec)
    candidate_path, candidate_content, candidate_state_hash = _candidate_state(artifact)
    selected_instruction_hash_loader = instruction_hash_loader or _instruction_hashes
    baseline_hash, candidate_hash = selected_instruction_hash_loader(
        spec.role, candidate_path
    )
    if baseline_hash != artifact.get("baseline_instruction_hash"):
        raise PromptEvaluationError("Baseline instruction hash differs from the build.")
    if candidate_hash != artifact.get("candidate_instruction_hash"):
        raise PromptEvaluationError(
            "Candidate instruction hash differs from the build."
        )
    if holdout.role != spec.role:
        raise PromptEvaluationError("Prompt holdout role differs from the candidate.")
    _, environment_hash = environment_identity(trusted_root)
    campaign_environment = campaign.get("environment_hash")
    if environment_hash != campaign_environment:
        raise PromptEvaluationError("Evaluator environment hash differs from campaign.")
    if (
        expected_environment_hash is not None
        and expected_environment_hash != campaign_environment
    ):
        raise PromptEvaluationError("CLI environment hash differs from campaign.")
    evaluator_identity, evaluator_hash = prompt_evaluator_identity(
        trusted_root, spec.role
    )
    state.bind_prompt_campaign_evaluator(campaign_id, evaluator_hash)
    holdout_hash = prompt_holdout_identity(holdout, holdout_oracle_hash)
    state.bind_prompt_campaign_holdout(campaign_id, holdout_hash)
    evaluation_id = state.create_evaluation(
        campaign_id=campaign_id,
        build_id=build_id,
        baseline_commit=str(campaign["baseline_commit"]),
        candidate_commit=candidate_hash,
        suite_id=f"prompt:{spec.dataset_manifest_hash}+{holdout.suite_id}",
        suite_hash=str(campaign["suite_hash"]),
        holdout_hash=holdout_hash,
        environment_hash=environment_hash,
        repetitions=repetitions,
        budget=asdict(budget),
    )
    identity = {
        "schema_version": PROMPT_EVALUATION_SCHEMA_VERSION,
        "artifact_kind": "prompt_evaluation_identity",
        "campaign_id": campaign_id,
        "build_id": build_id,
        "role": spec.role,
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
        "holdout_suite_hash": holdout.manifest_hash,
        "holdout_oracle_hash": holdout_oracle_hash,
        "evaluator": evaluator_identity,
        "evaluator_hash": evaluator_hash,
        "baseline_instruction_hash": baseline_hash,
        "candidate_instruction_hash": candidate_hash,
        "candidate_state_hash": candidate_state_hash,
        "activation": "not_performed",
        "promotion": "not_performed",
    }
    state.add_evaluation_artifact(
        evaluation_id,
        kind="prompt_program_state",
        content_hash=hash_text(candidate_content),
        content=candidate_content,
    )
    identity_content = json.dumps(identity, sort_keys=True)
    state.add_evaluation_artifact(
        evaluation_id,
        kind="prompt_evaluation_identity",
        content_hash=stable_hash(identity),
        content=identity_content,
    )
    if prediction_runner is None:
        prediction_runner, accounting = _runtime_prediction_runner(
            role=spec.role,
            candidate_path=candidate_path,
            budget=budget,
        )
    if accounting is None:
        accounting = lambda: {
            "hard_limit": budget.max_model_calls,
            "baseline": 0,
            "candidate": 0,
            "total": 0,
            "at_limit": False,
            "blocked_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_token_limit": budget.max_prompt_tokens,
            "completion_token_limit": budget.max_completion_tokens,
            "provider_retries_included": False,
        }
    development = _development_cases(spec)
    holdout_cases = [
        (case.case_id, case.inputs, holdout_oracle[case.case_id], 1.0)
        for case in holdout.cases
    ]
    development_ids = {case_id for case_id, *_ in development}
    holdout_ids = {case_id for case_id, *_ in holdout_cases}
    if development_ids & holdout_ids:
        raise PromptEvaluationError(
            "Prompt development and holdout case IDs must be disjoint."
        )
    results: list[PromptCaseResult] = []
    started = time.monotonic()
    try:
        for generation in ("baseline", "candidate"):
            for repetition in range(1, repetitions + 1):
                for visibility, cases in (
                    ("development", development),
                    ("holdout", holdout_cases),
                ):
                    for case_id, inputs, expected, audit_score in cases:
                        if time.monotonic() - started >= budget.campaign_wall_seconds:
                            raise PromptEvaluationError(
                                "Prompt evaluation wall-time budget exhausted."
                            )
                        case_started = time.perf_counter()
                        try:
                            actual = prediction_runner(generation, dict(inputs))
                            if not isinstance(actual, Mapping):
                                raise TypeError("prediction is not a mapping")
                            score, schema_valid = score_prompt_output(
                                spec.role,
                                expected,
                                actual,
                                audit_score=audit_score,
                            )
                            observation_hash = stable_hash(dict(actual))
                            failure = None if schema_valid else "schema"
                        except Exception as exc:
                            if isinstance(exc, PromptEvaluationError):
                                raise
                            if exc.__class__.__name__ == "ModelCallBudgetExceeded":
                                raise PromptEvaluationError(str(exc)) from exc
                            score = 0.0
                            schema_valid = False
                            observation_hash = None
                            failure = "prediction"
                        result = PromptCaseResult(
                            generation=generation,
                            repetition=repetition,
                            case_id=case_id,
                            visibility=visibility,
                            score=score,
                            schema_valid=schema_valid,
                            duration_ms=(time.perf_counter() - case_started) * 1000,
                            observation_hash=observation_hash,
                            failure=failure,
                        )
                        results.append(result)
                        state.add_evaluation_case(
                            evaluation_id,
                            generation=result.generation,
                            repetition=result.repetition,
                            case_id=result.case_id,
                            visibility=result.visibility,
                            result=result.to_dict(redact_holdout=False),
                        )
        model_call_accounting = accounting()
        evaluation = PromptPairedEvaluation(
            baseline_commit=str(campaign["baseline_commit"]),
            candidate_instruction_hash=candidate_hash,
            dataset_hash=spec.dataset_hash,
            dataset_manifest_hash=spec.dataset_manifest_hash,
            holdout_suite_hash=holdout.manifest_hash,
            holdout_oracle_hash=holdout_oracle_hash,
            environment_hash=environment_hash,
            evaluator_hash=evaluator_hash,
            repetitions=repetitions,
            budget=budget,
            model_call_accounting=model_call_accounting,
            results=tuple(results),
            evaluation_id=evaluation_id,
            build_id=build_id,
        )
        scorecard = build_prompt_scorecard(evaluation, artifact=artifact)
        state.complete_evaluation(
            evaluation_id,
            status="completed",
            scorecard=scorecard.to_dict(),
        )
    except Exception as exc:
        error = (
            exc
            if isinstance(exc, PromptEvaluationError)
            else PromptEvaluationError(
                f"Prompt evaluation failed: {type(exc).__name__}: {exc}"
            )
        )
        state.complete_evaluation(
            evaluation_id,
            status=("budget_exhausted" if "budget" in str(error).lower() else "failed"),
            scorecard={
                "error": type(error).__name__,
                "message": str(error),
            },
        )
        raise error from exc
    return evaluation, scorecard
