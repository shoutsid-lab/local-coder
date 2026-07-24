"""Collect and compare accuracy-first Qwythos profiles on Track G development cases."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from statistics import pstdev
from typing import Any

from runtime.dspy_lm import build_dspy_lm_with_profile
from runtime.route_contract_diagnostic import (
    DiagnosticError as ReportWriteError,
    HttpClient,
    inspect_service,
    write_report,
)
from runtime.route_profiles import RouteProfile, get_route_profile

from .outcomes import stable_hash
from .real_task_corpus import (
    DEFAULT_DEVELOPMENT_PATH,
    CaseSuite,
    RealTaskCase,
    load_case_suite,
)
from .real_task_development import (
    PLANNER_DIMENSIONS,
    PROMPT_STATE_FIELDS,
    REVIEWER_DIMENSIONS,
    ROLES,
    active_prompt_snapshot,
    git_commit,
    run_case,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "track-g-qwythos-tuning-v1.json"
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "real-task-evidence"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
COLLECTION_KIND = "real-task-profile-tuning-v1"
STORAGE_POLICY = (
    "development-only bounded scores; no generated planner/reviewer field text, "
    "final-answer text, prompt text, or reasoning text retained"
)
PROFILE_FIELDS = {field.name for field in fields(RouteProfile)}
ATTEMPT_FIELDS = {
    "case_id",
    "case_hash",
    "case_class",
    "difficulty",
    "pattern_group",
    "role",
    "attempt",
    "route",
    "adapter_success",
    "dimensions",
    "case_success",
    "score",
    "failure_codes",
    "prompt_tokens",
    "completion_tokens",
    "latency_seconds",
}
DIMENSION_FAILURES = {
    "scope_match": "scope_mismatch",
    "dependencies_match": "dependency_mismatch",
    "instruction_terms_match": "instruction_terms_missing",
    "acceptance_terms_match": "acceptance_terms_missing",
    "forbidden_files_absent": "forbidden_file_reference",
    "verdict_match": "verdict_mismatch",
    "required_issue_paths_found": "required_issue_path_missing",
    "required_unrelated_paths_found": "required_unrelated_path_missing",
    "forbidden_issue_paths_absent": "forbidden_issue_path_reported",
}


class ProfileTuningError(ValueError):
    """Raised when Qwythos tuning controls or evidence are invalid."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileTuningError(f"{name} must be a JSON object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileTuningError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProfileTuningError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise ProfileTuningError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _unit_interval(value: Any, name: str) -> float:
    result = _number(value, name)
    if result > 1.0:
        raise ProfileTuningError(f"{name} must be <= 1")
    return result


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileTuningError(f"{name} must be boolean")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if extra:
        details.append(f"unexpected {', '.join(extra)}")
    raise ProfileTuningError(f"{name} has invalid fields: {'; '.join(details)}")


def _load_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileTuningError(f"Unable to read {name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileTuningError(f"Invalid JSON in {name}: {path}") from exc


def _route_profile(value: Any, name: str, route: str) -> RouteProfile:
    data = _mapping(value, name)
    _exact_keys(data, PROFILE_FIELDS, name)
    try:
        profile = RouteProfile(**dict(data))
    except (TypeError, ValueError) as exc:
        raise ProfileTuningError(f"Invalid {name}: {exc}") from exc
    if profile.alias != route or profile.model_alias != route:
        raise ProfileTuningError(f"{name} must target {route}")
    if profile.reasoning_mode != "on" or profile.reasoning_tokens <= 0:
        raise ProfileTuningError(f"{name} must keep bounded reasoning enabled")
    if profile.preserve_reasoning:
        raise ProfileTuningError(f"{name} must not retain reasoning text")
    if not profile.requires_model_switch:
        raise ProfileTuningError(f"{name} must preserve operator-managed switching")
    return profile


@dataclass(frozen=True)
class HardGates:
    """Non-negotiable structural gates for a profile."""

    adapter_success_rate: float
    schema_rate: float
    minimum_case_score: float


@dataclass(frozen=True)
class HoldoutGate:
    """Accuracy thresholds required before opening role-specific holdout cases."""

    minimum_role_mean_gain: float
    minimum_overall_mean_score: float
    minimum_stable_case_success_rate: float
    material_regression_delta: float
    maximum_material_regressions: int


@dataclass(frozen=True)
class SelectionPolicy:
    """Frozen accuracy-first profile ranking and holdout policy."""

    control_profile: str
    accuracy_ranking: tuple[str, ...]
    hard_gates: HardGates
    holdout_gate: HoldoutGate


@dataclass(frozen=True)
class TuningProtocol:
    """Versioned Qwythos development-only profile experiment."""

    raw: Mapping[str, Any]
    protocol_id: str
    scoring_version: str
    suite_id: str
    suite_sha256: str
    attempts_per_case: int
    candidate_model_file: str
    llama_alias: str
    route: str
    profiles: Mapping[str, Mapping[str, RouteProfile]]
    selection_policy: SelectionPolicy

    @property
    def sha256(self) -> str:
        return stable_hash(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "TuningProtocol":
        data = _mapping(value, "protocol")
        _exact_keys(
            data,
            {
                "schema_version",
                "protocol_id",
                "scoring_version",
                "suite_id",
                "suite_sha256",
                "attempts_per_case",
                "candidate_model_file",
                "llama_alias",
                "route",
                "profiles",
                "selection_policy",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise ProfileTuningError("protocol.schema_version must be 1")
        route = _string(data["route"], "protocol.route")
        if route != "local-reason":
            raise ProfileTuningError("protocol.route must be local-reason")
        profile_data = _mapping(data["profiles"], "protocol.profiles")
        expected_profiles = {
            "current-control",
            "deterministic-accuracy",
            "role-depth-accuracy",
        }
        _exact_keys(profile_data, expected_profiles, "protocol.profiles")
        profiles: dict[str, dict[str, RouteProfile]] = {}
        for profile_id, role_values in profile_data.items():
            normalized_id = _string(profile_id, "protocol profile ID")
            roles = _mapping(role_values, f"protocol.profiles.{normalized_id}")
            _exact_keys(roles, set(ROLES), f"protocol.profiles.{normalized_id}")
            profiles[normalized_id] = {
                role: _route_profile(
                    roles[role],
                    f"protocol.profiles.{normalized_id}.{role}",
                    route,
                )
                for role in ROLES
            }
        policy = _selection_policy(data["selection_policy"], profiles)
        if asdict(profiles[policy.control_profile]["planner"]) != asdict(
            get_route_profile(route)
        ):
            raise ProfileTuningError(
                "selection_policy.control_profile must match the active local-reason "
                "profile"
            )
        if (
            profiles[policy.control_profile]["reviewer"]
            != profiles[policy.control_profile]["planner"]
        ):
            raise ProfileTuningError(
                "selection_policy.control_profile must use one current "
                "profile for both roles"
            )
        return cls(
            raw=dict(data),
            protocol_id=_string(data["protocol_id"], "protocol.protocol_id"),
            scoring_version=_string(
                data["scoring_version"], "protocol.scoring_version"
            ),
            suite_id=_string(data["suite_id"], "protocol.suite_id"),
            suite_sha256=_string(data["suite_sha256"], "protocol.suite_sha256"),
            attempts_per_case=_attempts_per_case(data["attempts_per_case"]),
            candidate_model_file=_string(
                data["candidate_model_file"], "protocol.candidate_model_file"
            ),
            llama_alias=_string(data["llama_alias"], "protocol.llama_alias"),
            route=route,
            profiles=profiles,
            selection_policy=policy,
        )


def _attempts_per_case(value: Any) -> int:
    attempts = _integer(value, "protocol.attempts_per_case", minimum=2)
    if attempts != 2:
        raise ProfileTuningError("protocol.attempts_per_case must be 2")
    return attempts


def _selection_policy(
    value: Any,
    profiles: Mapping[str, Mapping[str, RouteProfile]],
) -> SelectionPolicy:
    data = _mapping(value, "protocol.selection_policy")
    _exact_keys(
        data,
        {"control_profile", "accuracy_ranking", "hard_gates", "holdout_gate"},
        "protocol.selection_policy",
    )
    control = _string(data["control_profile"], "selection_policy.control_profile")
    if control not in profiles:
        raise ProfileTuningError("selection_policy.control_profile is unknown")
    ranking = data["accuracy_ranking"]
    expected_ranking = [
        "mean_score",
        "stable_case_success_rate",
        "minimum_score",
        "score_standard_deviation",
        "mean_latency_seconds",
    ]
    if ranking != expected_ranking:
        raise ProfileTuningError("selection_policy.accuracy_ranking is unsupported")
    hard = _mapping(data["hard_gates"], "selection_policy.hard_gates")
    _exact_keys(
        hard,
        {"adapter_success_rate", "schema_rate", "minimum_case_score"},
        "selection_policy.hard_gates",
    )
    holdout = _mapping(data["holdout_gate"], "selection_policy.holdout_gate")
    _exact_keys(
        holdout,
        {
            "minimum_role_mean_gain",
            "minimum_overall_mean_score",
            "minimum_stable_case_success_rate",
            "material_regression_delta",
            "maximum_material_regressions",
        },
        "selection_policy.holdout_gate",
    )
    hard_gates = HardGates(
        adapter_success_rate=_unit_interval(
            hard["adapter_success_rate"],
            "hard_gates.adapter_success_rate",
        ),
        schema_rate=_unit_interval(
            hard["schema_rate"],
            "hard_gates.schema_rate",
        ),
        minimum_case_score=_unit_interval(
            hard["minimum_case_score"],
            "hard_gates.minimum_case_score",
        ),
    )
    holdout_gate = HoldoutGate(
        minimum_role_mean_gain=_unit_interval(
            holdout["minimum_role_mean_gain"],
            "holdout_gate.minimum_role_mean_gain",
        ),
        minimum_overall_mean_score=_unit_interval(
            holdout["minimum_overall_mean_score"],
            "holdout_gate.minimum_overall_mean_score",
        ),
        minimum_stable_case_success_rate=_unit_interval(
            holdout["minimum_stable_case_success_rate"],
            "holdout_gate.minimum_stable_case_success_rate",
        ),
        material_regression_delta=_unit_interval(
            holdout["material_regression_delta"],
            "holdout_gate.material_regression_delta",
        ),
        maximum_material_regressions=_integer(
            holdout["maximum_material_regressions"],
            "holdout_gate.maximum_material_regressions",
        ),
    )
    return SelectionPolicy(
        control_profile=control,
        accuracy_ranking=tuple(expected_ranking),
        hard_gates=hard_gates,
        holdout_gate=holdout_gate,
    )


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> TuningProtocol:
    """Load the frozen G3 Qwythos profile-tuning protocol."""
    return TuningProtocol.from_mapping(_load_json(path, "profile-tuning protocol"))


def load_development_suite(protocol: TuningProtocol) -> CaseSuite:
    """Load the visible development suite and verify its frozen identity."""
    suite = load_case_suite(DEFAULT_DEVELOPMENT_PATH, expected_visibility="development")
    if suite.suite_id != protocol.suite_id:
        raise ProfileTuningError("Development suite ID does not match protocol")
    if suite.suite_hash != protocol.suite_sha256:
        raise ProfileTuningError("Development suite hash does not match protocol")
    return suite


def _profile_snapshot(
    profiles: Mapping[str, RouteProfile],
) -> dict[str, dict[str, Any]]:
    return {role: asdict(profiles[role]) for role in ROLES}


def _mean_known(records: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [record[field] for record in records if record[field] is not None]
    return None if not values else sum(values) / len(values)


def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        raise ProfileTuningError("Cannot summarize an empty record set")
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    failures: dict[str, int] = {}
    for record in records:
        by_case.setdefault(str(record["case_id"]), []).append(record)
        for failure in record["failure_codes"]:
            failures[failure] = failures.get(failure, 0) + 1
    scores = [float(record["score"]) for record in records]
    stable_successes = sum(
        all(bool(record["case_success"]) for record in case_records)
        for case_records in by_case.values()
    )
    return {
        "attempts": len(records),
        "cases": len(by_case),
        "failure_counts": dict(sorted(failures.items())),
        "adapter_success_rate": sum(
            bool(record["adapter_success"]) for record in records
        )
        / len(records),
        "schema_rate": sum(
            bool(record["dimensions"]["schema_valid"]) for record in records
        )
        / len(records),
        "case_success_rate": sum(bool(record["case_success"]) for record in records)
        / len(records),
        "stable_case_success_rate": stable_successes / len(by_case),
        "mean_score": sum(scores) / len(scores),
        "minimum_score": min(scores),
        "score_standard_deviation": pstdev(scores),
        "mean_prompt_tokens": _mean_known(records, "prompt_tokens"),
        "mean_completion_tokens": _mean_known(records, "completion_tokens"),
        "mean_latency_seconds": sum(
            float(record["latency_seconds"]) for record in records
        )
        / len(records),
        "total_latency_seconds": sum(
            float(record["latency_seconds"]) for record in records
        ),
    }


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize repeated profile attempts by role, case, and overall."""
    roles = {
        role: _metrics([record for record in records if record["role"] == role])
        for role in ROLES
    }
    case_ids = sorted({str(record["case_id"]) for record in records})
    cases = {
        case_id: _metrics(
            [record for record in records if record["case_id"] == case_id]
        )
        for case_id in case_ids
    }
    return {"overall": _metrics(records), "by_role": roles, "by_case": cases}


def _default_lm_factory(route: str, profile: RouteProfile) -> Any:
    return build_dspy_lm_with_profile(route, profile)


def _default_planner_runner(*, lm: Any, **inputs: Any) -> Any:
    from runtime.dspy_programs.planner import run_planner_program

    return run_planner_program(lm=lm, **inputs)


def _default_reviewer_runner(*, lm: Any, **inputs: Any) -> Any:
    from runtime.dspy_programs.reviewer import run_reviewer_program

    return run_reviewer_program(lm=lm, **inputs)


def collect_report(
    *,
    protocol: TuningProtocol,
    suite: CaseSuite,
    profile_id: str,
    environment_id: str,
    implementation_commit: str,
    service_identity: Mapping[str, Any],
    prompt_state: Mapping[str, Mapping[str, str] | None],
    lm_factory: Callable[[str, RouteProfile], Any] = _default_lm_factory,
    planner_runner: Callable[..., Any] = _default_planner_runner,
    reviewer_runner: Callable[..., Any] = _default_reviewer_runner,
) -> dict[str, Any]:
    """Run one frozen Qwythos profile over every development case twice."""
    try:
        profiles = protocol.profiles[profile_id]
    except KeyError as exc:
        raise ProfileTuningError(f"Unknown tuning profile: {profile_id}") from exc
    if suite.suite_id != protocol.suite_id or suite.suite_hash != protocol.suite_sha256:
        raise ProfileTuningError("Development suite does not match protocol")
    normalized_prompt_state = _normalize_prompt_state(prompt_state, "prompt_state")
    lms = {role: lm_factory(protocol.route, profiles[role]) for role in ROLES}
    runners = {"planner": planner_runner, "reviewer": reviewer_runner}
    records = [
        run_case(
            case=case,
            attempt=attempt,
            route=protocol.route,
            lm=lms[case.role],
            runner=runners[case.role],
        )
        for case in suite.cases
        for attempt in range(1, protocol.attempts_per_case + 1)
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "collection_kind": COLLECTION_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "profile_id": profile_id,
        "corpus_id": suite.corpus_id,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "environment_id": _string(environment_id, "environment_id"),
        "implementation_commit": _string(
            implementation_commit, "implementation_commit"
        ),
        "model_file": protocol.candidate_model_file,
        "llama_alias": protocol.llama_alias,
        "route": protocol.route,
        "role_profiles": _profile_snapshot(profiles),
        "active_prompt_state": normalized_prompt_state,
        "service_identity": dict(service_identity),
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "attempts": records,
        "summary": summarize(records),
        "storage_policy": STORAGE_POLICY,
        "holdout_loaded": False,
    }
    report["collection_sha256"] = stable_hash(report)
    return report


def _normalize_prompt_state(
    value: Any,
    name: str,
) -> dict[str, dict[str, str] | None]:
    data = _mapping(value, name)
    _exact_keys(data, set(ROLES), name)
    result: dict[str, dict[str, str] | None] = {}
    for role in ROLES:
        state = data[role]
        if state is None:
            result[role] = None
            continue
        fields_value = _mapping(state, f"{name}.{role}")
        _exact_keys(fields_value, set(PROMPT_STATE_FIELDS), f"{name}.{role}")
        result[role] = {
            field: _string(fields_value[field], f"{name}.{role}.{field}")
            for field in PROMPT_STATE_FIELDS
        }
    return result


def _expected_failures(
    *,
    adapter_success: bool,
    dimensions: Mapping[str, bool],
    role: str,
) -> list[str]:
    if not adapter_success:
        return ["adapter_error"]
    if not dimensions["schema_valid"]:
        return ["schema_mismatch"]
    names = PLANNER_DIMENSIONS if role == "planner" else REVIEWER_DIMENSIONS
    return [
        DIMENSION_FAILURES[name]
        for name in names
        if name != "schema_valid" and not dimensions[name]
    ]


def _validate_attempt(
    value: Any,
    *,
    case: RealTaskCase,
    attempt_number: int,
    route: str,
) -> dict[str, Any]:
    name = f"attempt[{case.case_id}:{attempt_number}]"
    data = _mapping(value, name)
    _exact_keys(data, ATTEMPT_FIELDS, name)
    expected_metadata = {
        "case_id": case.case_id,
        "case_hash": case.case_hash,
        "case_class": case.case_class,
        "difficulty": case.difficulty,
        "pattern_group": case.pattern_group,
        "role": case.role,
        "attempt": attempt_number,
        "route": route,
    }
    for field, expected in expected_metadata.items():
        if data[field] != expected:
            raise ProfileTuningError(f"{name}.{field} does not match")
    adapter_success = _boolean(data["adapter_success"], f"{name}.adapter_success")
    dimension_names = (
        PLANNER_DIMENSIONS if case.role == "planner" else REVIEWER_DIMENSIONS
    )
    dimensions_value = _mapping(data["dimensions"], f"{name}.dimensions")
    _exact_keys(dimensions_value, set(dimension_names), f"{name}.dimensions")
    dimensions = {
        field: _boolean(dimensions_value[field], f"{name}.dimensions.{field}")
        for field in dimension_names
    }
    if not adapter_success and any(dimensions.values()):
        raise ProfileTuningError(f"{name} failed adapter cannot pass dimensions")
    case_success = _boolean(data["case_success"], f"{name}.case_success")
    if case_success != (adapter_success and all(dimensions.values())):
        raise ProfileTuningError(f"{name}.case_success is inconsistent")
    score = _number(data["score"], f"{name}.score")
    expected_score = sum(dimensions.values()) / len(dimensions)
    if not math.isclose(score, expected_score, abs_tol=1e-12):
        raise ProfileTuningError(f"{name}.score is inconsistent")
    failures = data["failure_codes"]
    if not isinstance(failures, list) or not all(
        isinstance(item, str) and item for item in failures
    ):
        raise ProfileTuningError(f"{name}.failure_codes must be strings")
    if failures != _expected_failures(
        adapter_success=adapter_success,
        dimensions=dimensions,
        role=case.role,
    ):
        raise ProfileTuningError(f"{name}.failure_codes are inconsistent")
    for field in ("prompt_tokens", "completion_tokens"):
        if data[field] is not None:
            _integer(data[field], f"{name}.{field}")
    _number(data["latency_seconds"], f"{name}.latency_seconds")
    return dict(data)


def validate_report(
    value: Any,
    *,
    protocol: TuningProtocol,
    suite: CaseSuite,
) -> Mapping[str, Any]:
    """Validate one tuning report and rederive all attempts and summaries."""
    report = _mapping(value, "report")
    _exact_keys(
        report,
        {
            "schema_version",
            "collection_kind",
            "protocol_id",
            "protocol_sha256",
            "scoring_version",
            "profile_id",
            "corpus_id",
            "suite_id",
            "suite_sha256",
            "environment_id",
            "implementation_commit",
            "model_file",
            "llama_alias",
            "route",
            "role_profiles",
            "active_prompt_state",
            "service_identity",
            "collected_at_utc",
            "attempts",
            "summary",
            "storage_policy",
            "holdout_loaded",
            "collection_sha256",
        },
        "report",
    )
    if report["schema_version"] != 1 or report["collection_kind"] != COLLECTION_KIND:
        raise ProfileTuningError("report schema or collection kind is unsupported")
    if report["protocol_id"] != protocol.protocol_id:
        raise ProfileTuningError("report.protocol_id does not match")
    if report["protocol_sha256"] != protocol.sha256:
        raise ProfileTuningError("report.protocol_sha256 does not match")
    if report["scoring_version"] != protocol.scoring_version:
        raise ProfileTuningError("report.scoring_version does not match")
    if (
        report["corpus_id"] != suite.corpus_id
        or report["suite_id"] != suite.suite_id
        or report["suite_sha256"] != suite.suite_hash
    ):
        raise ProfileTuningError("report suite identity does not match")
    if report["holdout_loaded"] is not False:
        raise ProfileTuningError("Tuning report must not load holdout")
    expected_hash = _string(report["collection_sha256"], "report.collection_sha256")
    unhashed = dict(report)
    del unhashed["collection_sha256"]
    if expected_hash != stable_hash(unhashed):
        raise ProfileTuningError("report.collection_sha256 is invalid")
    profile_id = _string(report["profile_id"], "report.profile_id")
    try:
        expected_profiles = protocol.profiles[profile_id]
    except KeyError as exc:
        raise ProfileTuningError("report.profile_id is unknown") from exc
    if report["model_file"] != protocol.candidate_model_file:
        raise ProfileTuningError("report.model_file does not match protocol")
    if report["llama_alias"] != protocol.llama_alias:
        raise ProfileTuningError("report.llama_alias does not match protocol")
    if report["route"] != protocol.route:
        raise ProfileTuningError("report.route does not match protocol")
    if report["role_profiles"] != _profile_snapshot(expected_profiles):
        raise ProfileTuningError("report.role_profiles do not match protocol")
    _normalize_prompt_state(report["active_prompt_state"], "report.active_prompt_state")
    _string(report["environment_id"], "report.environment_id")
    commit = _string(report["implementation_commit"], "report.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise ProfileTuningError("report.implementation_commit must be full hash")
    try:
        timestamp = datetime.fromisoformat(
            _string(report["collected_at_utc"], "report.collected_at_utc")
        )
    except ValueError as exc:
        raise ProfileTuningError("report.collected_at_utc is invalid") from exc
    if timestamp.tzinfo is None:
        raise ProfileTuningError("report.collected_at_utc must include timezone")
    identity = _mapping(report["service_identity"], "report.service_identity")
    _exact_keys(
        identity,
        {
            "model_file",
            "llama_alias",
            "build_info",
            "configured_context_tokens",
            "total_slots",
        },
        "report.service_identity",
    )
    if identity["model_file"] != protocol.candidate_model_file:
        raise ProfileTuningError("report service model is inconsistent")
    if identity["llama_alias"] != protocol.llama_alias:
        raise ProfileTuningError("report service alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise ProfileTuningError("report build_info must be a string")
    _integer(identity["configured_context_tokens"], "report configured context")
    _integer(identity["total_slots"], "report total slots", minimum=1)
    attempts = report["attempts"]
    if not isinstance(attempts, list):
        raise ProfileTuningError("report.attempts must be an array")
    expected_count = len(suite.cases) * protocol.attempts_per_case
    if len(attempts) != expected_count:
        raise ProfileTuningError(
            f"report requires {expected_count} attempts, found {len(attempts)}"
        )
    validated: list[dict[str, Any]] = []
    offset = 0
    for case in suite.cases:
        for attempt_number in range(1, protocol.attempts_per_case + 1):
            validated.append(
                _validate_attempt(
                    attempts[offset],
                    case=case,
                    attempt_number=attempt_number,
                    route=protocol.route,
                )
            )
            offset += 1
    if report["summary"] != summarize(validated):
        raise ProfileTuningError("report.summary does not match attempts")
    if report["storage_policy"] != STORAGE_POLICY:
        raise ProfileTuningError("report.storage_policy is unsupported")
    return report


def _profile_eligible(metrics: Mapping[str, Any], gates: HardGates) -> bool:
    return (
        float(metrics["adapter_success_rate"]) >= gates.adapter_success_rate
        and float(metrics["schema_rate"]) >= gates.schema_rate
        and float(metrics["minimum_score"]) >= gates.minimum_case_score
    )


def _rank(metrics: Mapping[str, Any]) -> tuple[float, ...]:
    return (
        float(metrics["mean_score"]),
        float(metrics["stable_case_success_rate"]),
        float(metrics["minimum_score"]),
        -float(metrics["score_standard_deviation"]),
        -float(metrics["mean_latency_seconds"]),
    )


def _material_regressions(
    *,
    control: Mapping[str, Any],
    candidate: Mapping[str, Any],
    role: str,
    delta: float,
) -> list[dict[str, Any]]:
    regressions: list[dict[str, Any]] = []
    for case_id, control_metrics in control["summary"]["by_case"].items():
        control_attempt = next(
            attempt for attempt in control["attempts"] if attempt["case_id"] == case_id
        )
        if control_attempt["role"] != role:
            continue
        candidate_metrics = candidate["summary"]["by_case"][case_id]
        score_delta = float(candidate_metrics["mean_score"]) - float(
            control_metrics["mean_score"]
        )
        if score_delta < -delta:
            regressions.append(
                {
                    "case_id": case_id,
                    "control_mean_score": control_metrics["mean_score"],
                    "candidate_mean_score": candidate_metrics["mean_score"],
                    "delta": score_delta,
                }
            )
    return regressions


def compare_reports(
    values: Sequence[Any],
    *,
    protocol: TuningProtocol,
    suite: CaseSuite,
) -> dict[str, Any]:
    """Select accuracy-first role profiles and decide whether holdout may open."""
    validated = [
        validate_report(value, protocol=protocol, suite=suite) for value in values
    ]
    by_profile = {str(report["profile_id"]): report for report in validated}
    if len(by_profile) != len(validated):
        raise ProfileTuningError("Comparison contains duplicate profile reports")
    if set(by_profile) != set(protocol.profiles):
        missing = sorted(set(protocol.profiles) - set(by_profile))
        extra = sorted(set(by_profile) - set(protocol.profiles))
        raise ProfileTuningError(
            "Comparison requires every frozen profile; "
            f"missing={missing}, extra={extra}"
        )
    first = validated[0]
    for report in validated[1:]:
        for field in (
            "environment_id",
            "implementation_commit",
            "active_prompt_state",
            "service_identity",
            "suite_sha256",
            "protocol_sha256",
        ):
            if report[field] != first[field]:
                raise ProfileTuningError(f"Comparison reports differ in {field}")
    gates = protocol.selection_policy.hard_gates
    selected: dict[str, str] = {}
    for role in ROLES:
        eligible = [
            profile_id
            for profile_id, report in by_profile.items()
            if _profile_eligible(report["summary"]["by_role"][role], gates)
        ]
        if not eligible:
            raise ProfileTuningError(f"No {role} profile satisfies hard gates")
        selected[role] = max(
            eligible,
            key=lambda profile_id: _rank(
                by_profile[profile_id]["summary"]["by_role"][role]
            ),
        )
    overall_eligible = [
        profile_id
        for profile_id, report in by_profile.items()
        if _profile_eligible(report["summary"]["overall"], gates)
    ]
    if not overall_eligible:
        raise ProfileTuningError("No profile satisfies overall hard gates")
    selected["overall"] = max(
        overall_eligible,
        key=lambda profile_id: _rank(by_profile[profile_id]["summary"]["overall"]),
    )
    control = by_profile[protocol.selection_policy.control_profile]
    role_decisions: dict[str, Any] = {}
    selected_records: list[Mapping[str, Any]] = []
    open_roles: list[str] = []
    holdout_gate = protocol.selection_policy.holdout_gate
    for role in ROLES:
        chosen = by_profile[selected[role]]
        selected_records.extend(
            attempt for attempt in chosen["attempts"] if attempt["role"] == role
        )
        chosen_metrics = chosen["summary"]["by_role"][role]
        control_metrics = control["summary"]["by_role"][role]
        regressions = _material_regressions(
            control=control,
            candidate=chosen,
            role=role,
            delta=holdout_gate.material_regression_delta,
        )
        gain = float(chosen_metrics["mean_score"]) - float(
            control_metrics["mean_score"]
        )
        reasons: list[str] = []
        if not _profile_eligible(chosen_metrics, gates):
            reasons.append("hard_gate_failed")
        if gain + 1e-12 < holdout_gate.minimum_role_mean_gain:
            reasons.append("insufficient_role_mean_gain")
        if len(regressions) > holdout_gate.maximum_material_regressions:
            reasons.append("material_case_regression")
        ready = not reasons
        if ready:
            open_roles.append(role)
        role_decisions[role] = {
            "selected_profile": selected[role],
            "control_profile": protocol.selection_policy.control_profile,
            "control_mean_score": control_metrics["mean_score"],
            "selected_mean_score": chosen_metrics["mean_score"],
            "mean_score_gain": gain,
            "material_regressions": regressions,
            "holdout_ready": ready,
            "reasons": reasons,
        }
    mixed_summary = summarize(selected_records)
    combined_reasons: list[str] = []
    if float(mixed_summary["overall"]["mean_score"]) < (
        holdout_gate.minimum_overall_mean_score
    ):
        combined_reasons.append("insufficient_overall_mean_score")
    if float(mixed_summary["overall"]["stable_case_success_rate"]) < (
        holdout_gate.minimum_stable_case_success_rate
    ):
        combined_reasons.append("insufficient_stable_case_success_rate")
    if set(open_roles) != set(ROLES):
        combined_reasons.append("one_or_more_roles_not_ready")
    comparison: dict[str, Any] = {
        "schema_version": 1,
        "comparison_kind": "real-task-profile-selection-v1",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "environment_id": first["environment_id"],
        "implementation_commit": first["implementation_commit"],
        "service_identity": first["service_identity"],
        "report_hashes": {
            profile_id: report["collection_sha256"]
            for profile_id, report in sorted(by_profile.items())
        },
        "profile_summaries": {
            profile_id: report["summary"]
            for profile_id, report in sorted(by_profile.items())
        },
        "selected_profiles": selected,
        "role_decisions": role_decisions,
        "mixed_role_projection": mixed_summary,
        "holdout_gate": {
            "open_roles": open_roles,
            "combined_ready": not combined_reasons,
            "combined_reasons": combined_reasons,
        },
        "qualification_claim": None,
    }
    comparison["comparison_sha256"] = stable_hash(comparison)
    return comparison


def default_output_path(profile_id: str) -> Path:
    """Return a timestamped non-overwriting tuning-report path."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{profile_id}-track-g-tuning-v1-{timestamp}.json"


def _collect_parser(subparsers: Any) -> None:
    collect = subparsers.add_parser("collect", help="collect one frozen tuning profile")
    collect.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    collect.add_argument("--profile", required=True)
    collect.add_argument("--environment-id", required=True)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)


def _compare_parser(subparsers: Any) -> None:
    compare = subparsers.add_parser("compare", help="rank all frozen tuning profiles")
    compare.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    compare.add_argument("--reports", type=Path, nargs="+", required=True)
    compare.add_argument("--output", type=Path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    _collect_parser(subparsers)
    _compare_parser(subparsers)
    return result


def _collect(arguments: argparse.Namespace) -> int:
    protocol = load_protocol(arguments.protocol)
    suite = load_development_suite(protocol)
    if arguments.profile not in protocol.profiles:
        raise ProfileTuningError(f"Unknown tuning profile: {arguments.profile}")
    output = arguments.output or default_output_path(arguments.profile)
    prompt_state = active_prompt_snapshot()
    service_subject = build_service_subject(protocol)
    report = collect_report(
        protocol=protocol,
        suite=suite,
        profile_id=arguments.profile,
        environment_id=arguments.environment_id,
        implementation_commit=git_commit(),
        service_identity=inspect_service(
            client=HttpClient(),
            llama_base_url=arguments.llama_base_url,
            subject=service_subject,
        ),
        prompt_state=prompt_state,
    )
    if active_prompt_snapshot() != prompt_state:
        raise ProfileTuningError("Active prompt state changed during tuning collection")
    validate_report(report, protocol=protocol, suite=suite)
    write_report(output, report)
    print(
        json.dumps(
            {
                "output": str(output),
                "collection_sha256": report["collection_sha256"],
                "profile_id": report["profile_id"],
                "suite_sha256": report["suite_sha256"],
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


@dataclass(frozen=True)
class ServiceSubject:
    model_file: str
    llama_alias: str


def build_service_subject(protocol: TuningProtocol) -> ServiceSubject:
    """Return the minimal service identity expected by the shared inspector."""
    return ServiceSubject(
        model_file=protocol.candidate_model_file,
        llama_alias=protocol.llama_alias,
    )


def _compare(arguments: argparse.Namespace) -> int:
    protocol = load_protocol(arguments.protocol)
    suite = load_development_suite(protocol)
    reports = [_load_json(path, "tuning report") for path in arguments.reports]
    comparison = compare_reports(reports, protocol=protocol, suite=suite)
    if arguments.output is not None:
        write_report(arguments.output, comparison)
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "collect":
        return _collect(arguments)
    if arguments.command == "compare":
        return _compare(arguments)
    raise ProfileTuningError(f"Unsupported command: {arguments.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ProfileTuningError,
        ReportWriteError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"real-task-profile-tuning: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
