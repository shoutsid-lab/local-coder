"""Validate Qwythos qualification evidence and derive role decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .model_response import (
    EMPTY_COMPLETION,
    MALFORMED_FINAL,
    PROVIDER_ERROR,
    REASONING_ONLY_TRUNCATED,
    ROUTE_OK,
    TOOL_CALL_OK,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "profiles" / "qwythos-f3-qualification-v1.json"
ROLE_NAMES = ("planner", "reviewer")
SPLIT_NAMES = ("development", "holdout")
CONTRACT_SUITES = ("exact", "planner", "reviewer")
RESPONSE_OUTCOMES = frozenset(
    {
        ROUTE_OK,
        TOOL_CALL_OK,
        REASONING_ONLY_TRUNCATED,
        EMPTY_COMPLETION,
        MALFORMED_FINAL,
        PROVIDER_ERROR,
    }
)
_EPSILON = 1e-9


class QualificationError(ValueError):
    """Raised when policy or evidence is incomplete, malformed, or unbound."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{name} must be a JSON object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise QualificationError(f"{name} must be a JSON array")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationError(f"{name} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise QualificationError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise QualificationError(f"{name} must be an integer >= {minimum}")
    return value


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _finite_number(value: Any, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise QualificationError(f"{name} must be a finite number")
    return float(value)


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    result = _finite_number(value, name)
    if result < minimum:
        raise QualificationError(f"{name} must be >= {minimum}")
    return result


def _rate(value: Any, name: str) -> float:
    result = _number(value, name)
    if result > 1:
        raise QualificationError(f"{name} must be <= 1")
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise QualificationError(f"{name} has invalid fields: {'; '.join(details)}")


@dataclass(frozen=True)
class ContractSuitePolicy:
    """Frozen requirements for one focused response-contract suite."""

    minimum_attempts: int
    maximum_completion_tokens: int
    thinking_enabled: bool
    minimum_final_answer_rate: float
    minimum_schema_rate: float
    minimum_reasoning_presence_rate: float
    maximum_reasoning_presence_rate: float

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "ContractSuitePolicy":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {
                "minimum_attempts",
                "maximum_completion_tokens",
                "thinking_enabled",
                "minimum_final_answer_rate",
                "minimum_schema_rate",
                "minimum_reasoning_presence_rate",
                "maximum_reasoning_presence_rate",
            },
            name,
        )
        minimum_reasoning = _rate(
            data["minimum_reasoning_presence_rate"],
            f"{name}.minimum_reasoning_presence_rate",
        )
        maximum_reasoning = _rate(
            data["maximum_reasoning_presence_rate"],
            f"{name}.maximum_reasoning_presence_rate",
        )
        if minimum_reasoning > maximum_reasoning:
            raise QualificationError(
                f"{name} reasoning-presence minimum exceeds maximum"
            )
        return cls(
            minimum_attempts=_integer(
                data["minimum_attempts"], f"{name}.minimum_attempts", minimum=1
            ),
            maximum_completion_tokens=_integer(
                data["maximum_completion_tokens"],
                f"{name}.maximum_completion_tokens",
                minimum=1,
            ),
            thinking_enabled=_boolean(
                data["thinking_enabled"], f"{name}.thinking_enabled"
            ),
            minimum_final_answer_rate=_rate(
                data["minimum_final_answer_rate"],
                f"{name}.minimum_final_answer_rate",
            ),
            minimum_schema_rate=_rate(
                data["minimum_schema_rate"], f"{name}.minimum_schema_rate"
            ),
            minimum_reasoning_presence_rate=minimum_reasoning,
            maximum_reasoning_presence_rate=maximum_reasoning,
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Frozen limits for practical use on the target local machine."""

    maximum_startup_seconds: float
    maximum_model_switch_seconds: float
    maximum_peak_vram_mib: float
    maximum_peak_system_memory_mib: float
    minimum_context_tokens_tested: int
    minimum_generation_tokens_per_second: float
    maximum_p95_case_latency_seconds: float

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "ResourceLimits":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {
                "maximum_startup_seconds",
                "maximum_model_switch_seconds",
                "maximum_peak_vram_mib",
                "maximum_peak_system_memory_mib",
                "minimum_context_tokens_tested",
                "minimum_generation_tokens_per_second",
                "maximum_p95_case_latency_seconds",
            },
            name,
        )
        return cls(
            maximum_startup_seconds=_number(
                data["maximum_startup_seconds"],
                f"{name}.maximum_startup_seconds",
            ),
            maximum_model_switch_seconds=_number(
                data["maximum_model_switch_seconds"],
                f"{name}.maximum_model_switch_seconds",
            ),
            maximum_peak_vram_mib=_number(
                data["maximum_peak_vram_mib"], f"{name}.maximum_peak_vram_mib"
            ),
            maximum_peak_system_memory_mib=_number(
                data["maximum_peak_system_memory_mib"],
                f"{name}.maximum_peak_system_memory_mib",
            ),
            minimum_context_tokens_tested=_integer(
                data["minimum_context_tokens_tested"],
                f"{name}.minimum_context_tokens_tested",
                minimum=1,
            ),
            minimum_generation_tokens_per_second=_number(
                data["minimum_generation_tokens_per_second"],
                f"{name}.minimum_generation_tokens_per_second",
            ),
            maximum_p95_case_latency_seconds=_number(
                data["maximum_p95_case_latency_seconds"],
                f"{name}.maximum_p95_case_latency_seconds",
            ),
        )


@dataclass(frozen=True)
class RoleThresholds:
    """Frozen quality and regression gates applied to each role independently."""

    minimum_development_cases: int
    minimum_holdout_cases: int
    minimum_mean_score_delta: float
    maximum_case_score_regression: float
    maximum_mean_repair_iteration_delta: float

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "RoleThresholds":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {
                "minimum_development_cases",
                "minimum_holdout_cases",
                "minimum_mean_score_delta",
                "maximum_case_score_regression",
                "maximum_mean_repair_iteration_delta",
            },
            name,
        )
        return cls(
            minimum_development_cases=_integer(
                data["minimum_development_cases"],
                f"{name}.minimum_development_cases",
                minimum=1,
            ),
            minimum_holdout_cases=_integer(
                data["minimum_holdout_cases"],
                f"{name}.minimum_holdout_cases",
                minimum=1,
            ),
            minimum_mean_score_delta=_finite_number(
                data["minimum_mean_score_delta"],
                f"{name}.minimum_mean_score_delta",
            ),
            maximum_case_score_regression=_rate(
                data["maximum_case_score_regression"],
                f"{name}.maximum_case_score_regression",
            ),
            maximum_mean_repair_iteration_delta=_number(
                data["maximum_mean_repair_iteration_delta"],
                f"{name}.maximum_mean_repair_iteration_delta",
            ),
        )


_PROFILE_FIELDS = {
    "reasoning_mode",
    "max_tokens",
    "reasoning_tokens",
    "final_answer_tokens",
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
    "timeout_seconds",
    "retries",
}


def _parse_expected_profile(value: Any, name: str) -> dict[str, Any]:
    profile = _mapping(value, name)
    _exact_keys(profile, _PROFILE_FIELDS, name)
    if _string(profile["reasoning_mode"], f"{name}.reasoning_mode") != "on":
        raise QualificationError(f"{name}.reasoning_mode must be on")
    max_tokens = _integer(profile["max_tokens"], f"{name}.max_tokens", minimum=1)
    reasoning_tokens = _integer(
        profile["reasoning_tokens"], f"{name}.reasoning_tokens", minimum=1
    )
    final_answer_tokens = _integer(
        profile["final_answer_tokens"],
        f"{name}.final_answer_tokens",
        minimum=1,
    )
    if reasoning_tokens + final_answer_tokens > max_tokens:
        raise QualificationError(f"{name} token budgets exceed max_tokens")
    if _number(profile["temperature"], f"{name}.temperature") > 2:
        raise QualificationError(f"{name}.temperature must be <= 2")
    _rate(profile["top_p"], f"{name}.top_p")
    _integer(profile["top_k"], f"{name}.top_k", minimum=1)
    _number(profile["repetition_penalty"], f"{name}.repetition_penalty")
    _integer(profile["timeout_seconds"], f"{name}.timeout_seconds", minimum=1)
    _integer(profile["retries"], f"{name}.retries")
    return dict(profile)


@dataclass(frozen=True)
class QualificationPolicy:
    """Parsed versioned policy bound to one candidate route and model."""

    raw: Mapping[str, Any]
    policy_id: str
    candidate_model: str
    candidate_route: str
    baseline_routes: Mapping[str, str]
    expected_profiles: Mapping[str, Mapping[str, Any]]
    contract_suites: Mapping[str, ContractSuitePolicy]
    role_thresholds: RoleThresholds
    resource_limits: ResourceLimits

    @property
    def sha256(self) -> str:
        """Return the canonical hash used to bind evidence to this policy."""
        return _sha256(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "QualificationPolicy":
        data = _mapping(value, "policy")
        _exact_keys(
            data,
            {
                "schema_version",
                "policy_id",
                "candidate_model",
                "candidate_route",
                "baseline_routes",
                "expected_profiles",
                "contract_suites",
                "role_thresholds",
                "resource_limits",
            },
            "policy",
        )
        if data["schema_version"] != 1:
            raise QualificationError("policy.schema_version must be 1")

        baseline_routes = _mapping(data["baseline_routes"], "policy.baseline_routes")
        _exact_keys(baseline_routes, set(ROLE_NAMES), "policy.baseline_routes")
        parsed_baselines = {
            role: _string(baseline_routes[role], f"policy.baseline_routes.{role}")
            for role in ROLE_NAMES
        }

        expected_profiles = _mapping(
            data["expected_profiles"], "policy.expected_profiles"
        )
        _exact_keys(expected_profiles, set(ROLE_NAMES), "policy.expected_profiles")
        parsed_profiles = {
            role: _parse_expected_profile(
                expected_profiles[role], f"policy.expected_profiles.{role}"
            )
            for role in ROLE_NAMES
        }

        suites = _mapping(data["contract_suites"], "policy.contract_suites")
        _exact_keys(suites, set(CONTRACT_SUITES), "policy.contract_suites")
        parsed_suites = {
            suite: ContractSuitePolicy.from_mapping(
                suites[suite], f"policy.contract_suites.{suite}"
            )
            for suite in CONTRACT_SUITES
        }

        return cls(
            raw=dict(data),
            policy_id=_string(data["policy_id"], "policy.policy_id"),
            candidate_model=_string(data["candidate_model"], "policy.candidate_model"),
            candidate_route=_string(data["candidate_route"], "policy.candidate_route"),
            baseline_routes=parsed_baselines,
            expected_profiles=parsed_profiles,
            contract_suites=parsed_suites,
            role_thresholds=RoleThresholds.from_mapping(
                data["role_thresholds"], "policy.role_thresholds"
            ),
            resource_limits=ResourceLimits.from_mapping(
                data["resource_limits"], "policy.resource_limits"
            ),
        )


@dataclass(frozen=True)
class Decision:
    """Machine-readable qualification result for the candidate route."""

    outcome: str
    planner_qualified: bool
    reviewer_qualified: bool
    global_failures: tuple[str, ...]
    planner_failures: tuple[str, ...]
    reviewer_failures: tuple[str, ...]
    metrics: Mapping[str, Any]
    policy_sha256: str
    evidence_sha256: str

    def as_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable decision record."""
        return {
            "outcome": self.outcome,
            "planner_qualified": self.planner_qualified,
            "reviewer_qualified": self.reviewer_qualified,
            "global_failures": list(self.global_failures),
            "planner_failures": list(self.planner_failures),
            "reviewer_failures": list(self.reviewer_failures),
            "metrics": self.metrics,
            "policy_sha256": self.policy_sha256,
            "evidence_sha256": self.evidence_sha256,
        }


def load_json(path: Path, name: str) -> Mapping[str, Any]:
    """Load one JSON object with a concise qualification-specific error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"Unable to load {name} {path}: {exc}") from exc
    return _mapping(value, name)


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> QualificationPolicy:
    """Load and validate the frozen qualification policy."""
    return QualificationPolicy.from_mapping(load_json(path, "policy"))


def _validate_profiles(actual: Any, expected: Mapping[str, Mapping[str, Any]]) -> None:
    profiles = _mapping(actual, "evidence.candidate_profiles")
    _exact_keys(profiles, set(ROLE_NAMES), "evidence.candidate_profiles")
    for role in ROLE_NAMES:
        profile = _mapping(profiles[role], f"evidence.candidate_profiles.{role}")
        _exact_keys(
            profile,
            set(expected[role]),
            f"evidence.candidate_profiles.{role}",
        )
        if dict(profile) != dict(expected[role]):
            raise QualificationError(
                f"evidence.candidate_profiles.{role} does not match the policy"
            )


def _validate_common_record(data: Mapping[str, Any], name: str) -> None:
    outcome = _string(data["response_outcome"], f"{name}.response_outcome")
    if outcome not in RESPONSE_OUTCOMES:
        raise QualificationError(f"{name}.response_outcome is unsupported: {outcome}")
    _boolean(data["final_answer_present"], f"{name}.final_answer_present")
    _boolean(data["schema_valid"], f"{name}.schema_valid")
    _boolean(data["reasoning_present"], f"{name}.reasoning_present")
    _boolean(data["malformed_tool_call"], f"{name}.malformed_tool_call")
    _integer(data["prompt_tokens"], f"{name}.prompt_tokens")
    _integer(data["completion_tokens"], f"{name}.completion_tokens")
    _optional_integer(data["reasoning_tokens"], f"{name}.reasoning_tokens")
    _number(data["latency_seconds"], f"{name}.latency_seconds")
    _number(
        data["generated_tokens_per_second"],
        f"{name}.generated_tokens_per_second",
    )


def _validate_contract_runs(value: Any) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    expected = {
        "attempt_id",
        "suite",
        "thinking_enabled",
        "response_outcome",
        "final_answer_present",
        "schema_valid",
        "reasoning_present",
        "malformed_tool_call",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "latency_seconds",
        "generated_tokens_per_second",
    }
    for index, item in enumerate(_sequence(value, "evidence.contract_runs")):
        name = f"evidence.contract_runs[{index}]"
        data = _mapping(item, name)
        _exact_keys(data, expected, name)
        attempt_id = _string(data["attempt_id"], f"{name}.attempt_id")
        if attempt_id in seen:
            raise QualificationError(f"Duplicate contract attempt_id: {attempt_id}")
        seen.add(attempt_id)
        suite = _string(data["suite"], f"{name}.suite")
        if suite not in CONTRACT_SUITES:
            raise QualificationError(f"{name}.suite is unsupported: {suite}")
        _boolean(data["thinking_enabled"], f"{name}.thinking_enabled")
        _validate_common_record(data, name)
        records.append(data)
    return records


def _validate_role_cases(value: Any) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    expected = {
        "case_id",
        "role",
        "split",
        "baseline_score",
        "candidate_score",
        "baseline_success",
        "candidate_success",
        "baseline_repair_iterations",
        "candidate_repair_iterations",
        "out_of_scope_files",
        "response_outcome",
        "final_answer_present",
        "schema_valid",
        "reasoning_present",
        "malformed_tool_call",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "latency_seconds",
        "generated_tokens_per_second",
    }
    for index, item in enumerate(_sequence(value, "evidence.role_cases")):
        name = f"evidence.role_cases[{index}]"
        data = _mapping(item, name)
        _exact_keys(data, expected, name)
        case_id = _string(data["case_id"], f"{name}.case_id")
        role = _string(data["role"], f"{name}.role")
        split = _string(data["split"], f"{name}.split")
        if role not in ROLE_NAMES:
            raise QualificationError(f"{name}.role is unsupported: {role}")
        if split not in SPLIT_NAMES:
            raise QualificationError(f"{name}.split is unsupported: {split}")
        identity = (case_id, role, split)
        if identity in seen:
            raise QualificationError(
                f"Duplicate role case identity: {case_id}/{role}/{split}"
            )
        seen.add(identity)
        _rate(data["baseline_score"], f"{name}.baseline_score")
        _rate(data["candidate_score"], f"{name}.candidate_score")
        _boolean(data["baseline_success"], f"{name}.baseline_success")
        _boolean(data["candidate_success"], f"{name}.candidate_success")
        _integer(
            data["baseline_repair_iterations"],
            f"{name}.baseline_repair_iterations",
        )
        _integer(
            data["candidate_repair_iterations"],
            f"{name}.candidate_repair_iterations",
        )
        _integer(data["out_of_scope_files"], f"{name}.out_of_scope_files")
        _validate_common_record(data, name)
        records.append(data)
    return records


def _validate_resources(value: Any) -> Mapping[str, Any]:
    data = _mapping(value, "evidence.resources")
    _exact_keys(
        data,
        {
            "startup_seconds",
            "model_switch_seconds",
            "peak_vram_mib",
            "peak_system_memory_mib",
            "context_tokens_tested",
        },
        "evidence.resources",
    )
    _number(data["startup_seconds"], "evidence.resources.startup_seconds")
    _number(data["model_switch_seconds"], "evidence.resources.model_switch_seconds")
    _number(data["peak_vram_mib"], "evidence.resources.peak_vram_mib")
    _number(
        data["peak_system_memory_mib"],
        "evidence.resources.peak_system_memory_mib",
    )
    _integer(
        data["context_tokens_tested"],
        "evidence.resources.context_tokens_tested",
        minimum=1,
    )
    return data


def validate_evidence(
    value: Any, policy: QualificationPolicy
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Validate report shape and bind it to the frozen candidate and policy."""
    data = _mapping(value, "evidence")
    _exact_keys(
        data,
        {
            "schema_version",
            "policy_id",
            "policy_sha256",
            "candidate_model",
            "candidate_route",
            "candidate_profiles",
            "baseline_routes",
            "implementation_commit",
            "corpus_id",
            "corpus_sha256",
            "environment_id",
            "contract_runs",
            "role_cases",
            "resources",
        },
        "evidence",
    )
    if data["schema_version"] != 1:
        raise QualificationError("evidence.schema_version must be 1")
    if _string(data["policy_id"], "evidence.policy_id") != policy.policy_id:
        raise QualificationError("evidence.policy_id does not match the policy")
    if _string(data["policy_sha256"], "evidence.policy_sha256") != policy.sha256:
        raise QualificationError("evidence.policy_sha256 does not match the policy")
    if (
        _string(data["candidate_model"], "evidence.candidate_model")
        != policy.candidate_model
    ):
        raise QualificationError("evidence.candidate_model does not match the policy")
    if (
        _string(data["candidate_route"], "evidence.candidate_route")
        != policy.candidate_route
    ):
        raise QualificationError("evidence.candidate_route does not match the policy")
    _validate_profiles(data["candidate_profiles"], policy.expected_profiles)

    baseline_routes = _mapping(data["baseline_routes"], "evidence.baseline_routes")
    _exact_keys(baseline_routes, set(ROLE_NAMES), "evidence.baseline_routes")
    if dict(baseline_routes) != dict(policy.baseline_routes):
        raise QualificationError(
            "evidence.baseline_routes do not match the frozen policy"
        )

    commit = _string(data["implementation_commit"], "evidence.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise QualificationError(
            "evidence.implementation_commit must be a lowercase 40-character SHA"
        )
    _string(data["corpus_id"], "evidence.corpus_id")
    corpus_hash = _string(data["corpus_sha256"], "evidence.corpus_sha256")
    if len(corpus_hash) != 64 or any(
        character not in "0123456789abcdef" for character in corpus_hash
    ):
        raise QualificationError("evidence.corpus_sha256 must be a lowercase SHA-256")
    _string(data["environment_id"], "evidence.environment_id")

    contract_runs = _validate_contract_runs(data["contract_runs"])
    role_cases = _validate_role_cases(data["role_cases"])
    resources = _validate_resources(data["resources"])
    return resources, contract_runs, role_cases


def _ratio(records: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(bool(record[field]) for record in records) / len(records)


def _mean(records: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(float(record[field]) for record in records) / len(records)


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _generation_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reasoning_values = [
        int(record["reasoning_tokens"])
        for record in records
        if record["reasoning_tokens"] is not None
    ]
    prompt_tokens = sum(int(record["prompt_tokens"]) for record in records)
    completion_tokens = sum(int(record["completion_tokens"]) for record in records)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "provider_total_tokens": prompt_tokens + completion_tokens,
        "reasoning_tokens_reported": len(reasoning_values),
        "reasoning_tokens": sum(reasoning_values),
        "minimum_generation_tokens_per_second": min(
            float(record["generated_tokens_per_second"]) for record in records
        ),
        "p95_latency_seconds": _p95(
            [float(record["latency_seconds"]) for record in records]
        ),
    }


def _contract_failures(
    records: Sequence[Mapping[str, Any]], policy: QualificationPolicy
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    metrics: dict[str, Any] = {}
    for suite in CONTRACT_SUITES:
        suite_records = [record for record in records if record["suite"] == suite]
        suite_policy = policy.contract_suites[suite]
        if len(suite_records) < suite_policy.minimum_attempts:
            failures.append(
                f"contract.{suite}: requires at least "
                f"{suite_policy.minimum_attempts} attempts, found {len(suite_records)}"
            )
            continue
        final_rate = _ratio(suite_records, "final_answer_present")
        schema_rate = _ratio(suite_records, "schema_valid")
        reasoning_rate = _ratio(suite_records, "reasoning_present")
        outcomes = Counter(str(record["response_outcome"]) for record in suite_records)
        malformed_tool_calls = sum(
            bool(record["malformed_tool_call"]) for record in suite_records
        )
        thinking_values = {bool(record["thinking_enabled"]) for record in suite_records}
        over_budget = [
            str(record["attempt_id"])
            for record in suite_records
            if int(record["completion_tokens"]) > suite_policy.maximum_completion_tokens
        ]
        metrics[suite] = {
            "attempts": len(suite_records),
            "final_answer_rate": final_rate,
            "schema_rate": schema_rate,
            "reasoning_presence_rate": reasoning_rate,
            "response_outcomes": dict(sorted(outcomes.items())),
            "malformed_tool_calls": malformed_tool_calls,
            "over_budget_attempts": over_budget,
            **_generation_metrics(suite_records),
        }
        if thinking_values != {suite_policy.thinking_enabled}:
            failures.append(
                f"contract.{suite}: thinking control does not match the frozen policy"
            )
        if final_rate < suite_policy.minimum_final_answer_rate:
            failures.append(
                f"contract.{suite}: final-answer rate {final_rate:.3f} is below "
                f"{suite_policy.minimum_final_answer_rate:.3f}"
            )
        if schema_rate < suite_policy.minimum_schema_rate:
            failures.append(
                f"contract.{suite}: schema rate {schema_rate:.3f} is below "
                f"{suite_policy.minimum_schema_rate:.3f}"
            )
        if not (
            suite_policy.minimum_reasoning_presence_rate
            <= reasoning_rate
            <= suite_policy.maximum_reasoning_presence_rate
        ):
            failures.append(
                f"contract.{suite}: reasoning-presence rate {reasoning_rate:.3f} "
                "is outside the frozen range"
            )
        non_success = sum(
            count for outcome, count in outcomes.items() if outcome != ROUTE_OK
        )
        if non_success:
            failures.append(
                f"contract.{suite}: {non_success} response(s) were not route_ok"
            )
        if malformed_tool_calls:
            failures.append(
                f"contract.{suite}: {malformed_tool_calls} malformed tool call(s)"
            )
        if over_budget:
            failures.append(
                f"contract.{suite}: completion budget exceeded: "
                + ", ".join(over_budget)
            )
    return failures, metrics


def _resource_failures(
    resources: Mapping[str, Any],
    contract_records: Sequence[Mapping[str, Any]],
    role_records: Sequence[Mapping[str, Any]],
    policy: QualificationPolicy,
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    limits = policy.resource_limits
    all_records = [*contract_records, *role_records]
    if not all_records:
        return ["resources: no timed generation records were supplied"], {}
    if not role_records:
        return ["resources: no timed role cases were supplied"], {}
    minimum_throughput = min(
        float(record["generated_tokens_per_second"]) for record in all_records
    )
    p95_latency = _p95([float(record["latency_seconds"]) for record in role_records])
    metrics = {
        "startup_seconds": float(resources["startup_seconds"]),
        "model_switch_seconds": float(resources["model_switch_seconds"]),
        "peak_vram_mib": float(resources["peak_vram_mib"]),
        "peak_system_memory_mib": float(resources["peak_system_memory_mib"]),
        "context_tokens_tested": int(resources["context_tokens_tested"]),
        "minimum_generation_tokens_per_second": minimum_throughput,
        "p95_case_latency_seconds": p95_latency,
    }
    checks = (
        (
            metrics["startup_seconds"] <= limits.maximum_startup_seconds,
            "resources: startup time exceeds the frozen limit",
        ),
        (
            metrics["model_switch_seconds"] <= limits.maximum_model_switch_seconds,
            "resources: model-switch time exceeds the frozen limit",
        ),
        (
            metrics["peak_vram_mib"] <= limits.maximum_peak_vram_mib,
            "resources: peak VRAM exceeds the frozen limit",
        ),
        (
            metrics["peak_system_memory_mib"] <= limits.maximum_peak_system_memory_mib,
            "resources: peak system memory exceeds the frozen limit",
        ),
        (
            metrics["context_tokens_tested"] >= limits.minimum_context_tokens_tested,
            "resources: tested context is below the frozen minimum",
        ),
        (
            minimum_throughput >= limits.minimum_generation_tokens_per_second,
            "resources: generation throughput is below the frozen minimum",
        ),
        (
            p95_latency <= limits.maximum_p95_case_latency_seconds,
            "resources: p95 case latency exceeds the frozen limit",
        ),
    )
    failures.extend(message for passed, message in checks if not passed)
    return failures, metrics


def _role_failures(
    role: str,
    records: Sequence[Mapping[str, Any]],
    thresholds: RoleThresholds,
    maximum_completion_tokens: int,
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    metrics: dict[str, Any] = {}
    for split in SPLIT_NAMES:
        split_records = [
            record
            for record in records
            if record["role"] == role and record["split"] == split
        ]
        minimum = (
            thresholds.minimum_development_cases
            if split == "development"
            else thresholds.minimum_holdout_cases
        )
        if len(split_records) < minimum:
            failures.append(
                f"{role}.{split}: requires at least {minimum} cases, "
                f"found {len(split_records)}"
            )
            continue

        baseline_mean = _mean(split_records, "baseline_score")
        candidate_mean = _mean(split_records, "candidate_score")
        score_delta = candidate_mean - baseline_mean
        baseline_success_rate = _ratio(split_records, "baseline_success")
        candidate_success_rate = _ratio(split_records, "candidate_success")
        repair_delta = _mean(split_records, "candidate_repair_iterations") - _mean(
            split_records, "baseline_repair_iterations"
        )
        regressions = [
            str(record["case_id"])
            for record in split_records
            if float(record["baseline_score"]) - float(record["candidate_score"])
            > thresholds.maximum_case_score_regression + _EPSILON
        ]
        lost_successes = [
            str(record["case_id"])
            for record in split_records
            if record["baseline_success"] and not record["candidate_success"]
        ]
        invalid_contracts = [
            str(record["case_id"])
            for record in split_records
            if record["response_outcome"] != ROUTE_OK
            or not record["final_answer_present"]
            or not record["schema_valid"]
            or not record["reasoning_present"]
            or record["malformed_tool_call"]
        ]
        over_budget = [
            str(record["case_id"])
            for record in split_records
            if int(record["completion_tokens"]) > maximum_completion_tokens
        ]
        out_of_scope = [
            str(record["case_id"])
            for record in split_records
            if int(record["out_of_scope_files"]) > 0
        ]

        metrics[split] = {
            "cases": len(split_records),
            "baseline_mean_score": baseline_mean,
            "candidate_mean_score": candidate_mean,
            "mean_score_delta": score_delta,
            "baseline_success_rate": baseline_success_rate,
            "candidate_success_rate": candidate_success_rate,
            "mean_repair_iteration_delta": repair_delta,
            "material_regressions": regressions,
            "lost_successes": lost_successes,
            "invalid_contract_cases": invalid_contracts,
            "over_budget_cases": over_budget,
            "out_of_scope_cases": out_of_scope,
            **_generation_metrics(split_records),
        }
        if score_delta + _EPSILON < thresholds.minimum_mean_score_delta:
            failures.append(
                f"{role}.{split}: mean score delta {score_delta:.3f} is below "
                f"{thresholds.minimum_mean_score_delta:.3f}"
            )
        if candidate_success_rate + _EPSILON < baseline_success_rate:
            failures.append(f"{role}.{split}: candidate success rate regressed")
        if repair_delta > thresholds.maximum_mean_repair_iteration_delta + _EPSILON:
            failures.append(
                f"{role}.{split}: mean repair-iteration delta {repair_delta:.3f} "
                "exceeds the frozen limit"
            )
        if regressions:
            failures.append(
                f"{role}.{split}: material case regressions: " + ", ".join(regressions)
            )
        if lost_successes:
            failures.append(
                f"{role}.{split}: baseline successes lost: " + ", ".join(lost_successes)
            )
        if invalid_contracts:
            failures.append(
                f"{role}.{split}: invalid final/schema contract: "
                + ", ".join(invalid_contracts)
            )
        if over_budget:
            failures.append(
                f"{role}.{split}: completion budget exceeded: " + ", ".join(over_budget)
            )
        if out_of_scope:
            failures.append(
                f"{role}.{split}: out-of-scope changes: " + ", ".join(out_of_scope)
            )
    return failures, metrics


def evaluate_qualification(evidence: Any, policy: QualificationPolicy) -> Decision:
    """Validate evidence and derive independent planner and reviewer decisions."""
    resources, contract_runs, role_cases = validate_evidence(evidence, policy)
    contract_failures, contract_metrics = _contract_failures(contract_runs, policy)
    resource_failures, resource_metrics = _resource_failures(
        resources, contract_runs, role_cases, policy
    )
    global_failures = tuple(contract_failures + resource_failures)

    planner_failures, planner_metrics = _role_failures(
        "planner",
        role_cases,
        policy.role_thresholds,
        int(policy.expected_profiles["planner"]["max_tokens"]),
    )
    reviewer_failures, reviewer_metrics = _role_failures(
        "reviewer",
        role_cases,
        policy.role_thresholds,
        int(policy.expected_profiles["reviewer"]["max_tokens"]),
    )
    planner_qualified = not global_failures and not planner_failures
    reviewer_qualified = not global_failures and not reviewer_failures

    if planner_qualified and reviewer_qualified:
        outcome = "qualified_for_both"
    elif planner_qualified:
        outcome = "qualified_for_planner_only"
    elif reviewer_qualified:
        outcome = "qualified_for_reviewer_only"
    elif not global_failures:
        outcome = "diagnostic_only"
    else:
        outcome = "rejected"

    metrics = {
        "contract": contract_metrics,
        "resources": resource_metrics,
        "planner": planner_metrics,
        "reviewer": reviewer_metrics,
    }
    return Decision(
        outcome=outcome,
        planner_qualified=planner_qualified,
        reviewer_qualified=reviewer_qualified,
        global_failures=global_failures,
        planner_failures=tuple(planner_failures),
        reviewer_failures=tuple(reviewer_failures),
        metrics=metrics,
        policy_sha256=policy.sha256,
        evidence_sha256=_sha256(evidence),
    )


def parser() -> argparse.ArgumentParser:
    """Build the qualification-report command parser."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "evidence",
        type=Path,
        nargs="?",
        help="Machine-readable F3 evidence report.",
    )
    result.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help="Frozen qualification policy JSON.",
    )
    result.add_argument(
        "--print-policy-hash",
        action="store_true",
        help="Print the canonical policy SHA-256 and exit.",
    )
    result.add_argument(
        "--require",
        choices=("any", "planner", "reviewer", "both"),
        help="Exit non-zero unless the requested role outcome qualifies.",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Validate evidence, print a bounded decision, and enforce optional outcome."""
    arguments = parser().parse_args(argv)
    policy = load_policy(arguments.policy)
    if arguments.print_policy_hash:
        print(policy.sha256)
        return 0
    if arguments.evidence is None:
        raise QualificationError("evidence path is required")
    evidence = load_json(arguments.evidence, "evidence")
    decision = evaluate_qualification(evidence, policy)
    print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))

    required: Literal["any", "planner", "reviewer", "both"] | None = arguments.require
    if required is None:
        return 0
    passed = {
        "any": decision.planner_qualified or decision.reviewer_qualified,
        "planner": decision.planner_qualified,
        "reviewer": decision.reviewer_qualified,
        "both": decision.planner_qualified and decision.reviewer_qualified,
    }[required]
    return 0 if passed else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationError as exc:
        print(f"route-qualification: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
