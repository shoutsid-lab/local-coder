"""Collect per-case planner/reviewer evidence on the frozen Track G development set."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.dspy_lm import build_dspy_lm
from runtime.prompt_activation import PromptActivationError, read_active_prompt
from runtime.route_contract_diagnostic import (
    DiagnosticError as ReportWriteError,
    HttpClient,
    inspect_service,
    write_report,
)
from runtime.route_profiles import get_route_profile

from .outcomes import stable_hash
from .real_task_corpus import (
    DEFAULT_DEVELOPMENT_PATH,
    CaseSuite,
    RealTaskCase,
    load_case_suite,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "track-g-development-v1.json"
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "real-task-evidence"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
SUBJECTS = ("baseline", "candidate")
ROLES = ("planner", "reviewer")
PROMPT_STATE_FIELDS = (
    "activation_id",
    "campaign_id",
    "build_id",
    "evaluation_id",
    "candidate_instruction_hash",
    "program_hash",
)
PLANNER_DIMENSIONS = (
    "schema_valid",
    "scope_match",
    "dependencies_match",
    "instruction_terms_match",
    "acceptance_terms_match",
    "forbidden_files_absent",
)
REVIEWER_DIMENSIONS = (
    "schema_valid",
    "verdict_match",
    "required_issue_paths_found",
    "required_unrelated_paths_found",
    "forbidden_issue_paths_absent",
)
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
STORAGE_POLICY = (
    "no generated planner/reviewer field text, final-answer text, "
    "or reasoning text retained"
)


class RealTaskDevelopmentError(ValueError):
    """Raised when Track G development evidence is malformed or incomparable."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RealTaskDevelopmentError(f"{name} must be a JSON object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RealTaskDevelopmentError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RealTaskDevelopmentError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise RealTaskDevelopmentError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RealTaskDevelopmentError(f"{name} must be boolean")
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
    raise RealTaskDevelopmentError(f"{name} has invalid fields: {'; '.join(details)}")


def _load_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RealTaskDevelopmentError(f"Unable to read {name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RealTaskDevelopmentError(f"Invalid JSON in {name}: {path}") from exc


def route_profile_snapshot(route: str) -> dict[str, Any]:
    """Bind collection to the exact generation profile for one logical route."""
    try:
        return asdict(get_route_profile(route))
    except KeyError as exc:
        raise RealTaskDevelopmentError(f"Unknown route: {route}") from exc


@dataclass(frozen=True)
class Subject:
    """One model and its planner/reviewer routes."""

    model_file: str
    llama_alias: str
    routes: Mapping[str, str]
    route_profiles: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "Subject":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {"model_file", "llama_alias", "routes", "route_profiles"},
            name,
        )
        routes = _mapping(data["routes"], f"{name}.routes")
        profiles = _mapping(data["route_profiles"], f"{name}.route_profiles")
        _exact_keys(routes, set(ROLES), f"{name}.routes")
        _exact_keys(profiles, set(ROLES), f"{name}.route_profiles")
        parsed_routes = {
            role: _string(routes[role], f"{name}.routes.{role}") for role in ROLES
        }
        parsed_profiles = {
            role: dict(_mapping(profiles[role], f"{name}.route_profiles.{role}"))
            for role in ROLES
        }
        for role, route in parsed_routes.items():
            if parsed_profiles[role] != route_profile_snapshot(route):
                raise RealTaskDevelopmentError(
                    f"{name}.route_profiles.{role} does not match {route}"
                )
        return cls(
            model_file=_string(data["model_file"], f"{name}.model_file"),
            llama_alias=_string(data["llama_alias"], f"{name}.llama_alias"),
            routes=parsed_routes,
            route_profiles=parsed_profiles,
        )


@dataclass(frozen=True)
class Protocol:
    """Versioned development-set collection contract."""

    raw: Mapping[str, Any]
    protocol_id: str
    scoring_version: str
    suite_id: str
    suite_sha256: str
    attempts_per_case: int
    subjects: Mapping[str, Subject]

    @property
    def sha256(self) -> str:
        return stable_hash(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "Protocol":
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
                "subjects",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise RealTaskDevelopmentError("protocol.schema_version must be 1")
        subjects = _mapping(data["subjects"], "protocol.subjects")
        _exact_keys(subjects, set(SUBJECTS), "protocol.subjects")
        return cls(
            raw=dict(data),
            protocol_id=_string(data["protocol_id"], "protocol.protocol_id"),
            scoring_version=_string(
                data["scoring_version"], "protocol.scoring_version"
            ),
            suite_id=_string(data["suite_id"], "protocol.suite_id"),
            suite_sha256=_string(data["suite_sha256"], "protocol.suite_sha256"),
            attempts_per_case=_integer(
                data["attempts_per_case"],
                "protocol.attempts_per_case",
                minimum=1,
            ),
            subjects={
                name: Subject.from_mapping(subjects[name], f"protocol.subjects.{name}")
                for name in SUBJECTS
            },
        )


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> Protocol:
    """Load the frozen Track G development protocol."""
    return Protocol.from_mapping(_load_json(path, "development protocol"))


def load_development_suite(
    protocol: Protocol,
    path: Path = DEFAULT_DEVELOPMENT_PATH,
) -> CaseSuite:
    """Load only the candidate-visible development suite and bind its hash."""
    suite = load_case_suite(path, expected_visibility="development")
    if suite.suite_id != protocol.suite_id:
        raise RealTaskDevelopmentError("Development suite ID does not match protocol")
    if suite.suite_hash != protocol.suite_sha256:
        raise RealTaskDevelopmentError("Development suite hash does not match protocol")
    return suite


def git_commit(root: Path = ROOT) -> str:
    """Return HEAD only when collection runs from a committed, clean tree."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RealTaskDevelopmentError(
            "Collection requires a committed, clean working tree"
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(commit) != 40:
        raise RealTaskDevelopmentError("Unable to resolve a full commit hash")
    return commit


def active_prompt_snapshot() -> dict[str, dict[str, str] | None]:
    """Bind reports to active prompt lineage without retaining prompt text."""
    snapshot: dict[str, dict[str, str] | None] = {}
    for role in ROLES:
        try:
            pointer = read_active_prompt(role)
        except PromptActivationError as exc:
            raise RealTaskDevelopmentError(
                f"Unable to bind active {role} prompt state"
            ) from exc
        if pointer is None:
            snapshot[role] = None
            continue
        state = {field: pointer.get(field) for field in PROMPT_STATE_FIELDS}
        if any(not isinstance(item, str) or not item for item in state.values()):
            raise RealTaskDevelopmentError(
                f"Active {role} prompt lineage is incomplete"
            )
        snapshot[role] = state
    return snapshot


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
        fields = _mapping(state, f"{name}.{role}")
        _exact_keys(fields, set(PROMPT_STATE_FIELDS), f"{name}.{role}")
        result[role] = {
            field: _string(fields[field], f"{name}.{role}.{field}")
            for field in PROMPT_STATE_FIELDS
        }
    return result


def _prediction_value(prediction: Any, name: str) -> Any:
    if isinstance(prediction, Mapping):
        return prediction[name]
    return getattr(prediction, name)


def _prediction_usage(prediction: Any, route: str) -> tuple[int | None, int | None]:
    get_usage = getattr(prediction, "get_lm_usage", None)
    if not callable(get_usage):
        return None, None
    usage_by_lm = get_usage() or {}
    usage = usage_by_lm.get(f"openai/{route}")
    if not isinstance(usage, Mapping):
        usage = next(
            (item for item in usage_by_lm.values() if isinstance(item, Mapping)),
            {},
        )
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else None,
        (
            completion
            if isinstance(completion, int) and not isinstance(completion, bool)
            else None
        ),
    )


def _valid_string_list(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> bool:
    if not isinstance(value, list):
        return False
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value)


def _contains_all(text: str, terms: Sequence[str]) -> bool:
    lowered = text.casefold()
    return all(term.casefold() in lowered for term in terms)


def _paths_found(items: Sequence[str], paths: Sequence[str]) -> bool:
    lowered = [item.casefold() for item in items]
    return all(any(path.casefold() in item for item in lowered) for path in paths)


def _paths_absent(items: Sequence[str], paths: Sequence[str]) -> bool:
    lowered = [item.casefold() for item in items]
    return all(not any(path.casefold() in item for item in lowered) for path in paths)


def _assessment(
    *,
    role: str,
    dimensions: Mapping[str, bool],
) -> dict[str, Any]:
    names = PLANNER_DIMENSIONS if role == "planner" else REVIEWER_DIMENSIONS
    normalized = {name: bool(dimensions[name]) for name in names}
    if not normalized["schema_valid"]:
        failures = ["schema_mismatch"]
    else:
        failures = [
            DIMENSION_FAILURES[name]
            for name in names
            if name != "schema_valid" and not normalized[name]
        ]
    return {
        "dimensions": normalized,
        "case_success": all(normalized.values()),
        "score": sum(normalized.values()) / len(normalized),
        "failure_codes": failures,
    }


def assess_planner_prediction(
    prediction: Any,
    oracle: Mapping[str, Any],
) -> dict[str, Any]:
    """Score one PlannerProgram result against a frozen development oracle."""
    try:
        instruction = _prediction_value(prediction, "instruction")
        editable_files = _prediction_value(prediction, "editable_files")
        acceptance_criteria = _prediction_value(prediction, "acceptance_criteria")
        depends_on = _prediction_value(prediction, "depends_on")
    except (AttributeError, KeyError, TypeError):
        return _assessment(
            role="planner",
            dimensions={name: False for name in PLANNER_DIMENSIONS},
        )
    schema = (
        isinstance(instruction, str)
        and bool(instruction.strip())
        and _valid_string_list(editable_files, minimum=1, maximum=2)
        and _valid_string_list(acceptance_criteria, minimum=1, maximum=6)
        and _valid_string_list(depends_on, maximum=6)
    )
    if not schema:
        return _assessment(
            role="planner",
            dimensions={name: False for name in PLANNER_DIMENSIONS},
        )
    generated_text = "\n".join([instruction, *acceptance_criteria])
    expected_files = set(oracle["editable_files"])
    dimensions = {
        "schema_valid": True,
        "scope_match": set(editable_files) == expected_files
        and len(editable_files) == len(expected_files),
        "dependencies_match": depends_on == list(oracle["depends_on"]),
        "instruction_terms_match": _contains_all(
            instruction, oracle["required_instruction_terms"]
        ),
        "acceptance_terms_match": _contains_all(
            "\n".join(acceptance_criteria),
            oracle["required_acceptance_terms"],
        ),
        "forbidden_files_absent": _paths_absent(
            [*editable_files, generated_text],
            oracle["forbidden_files"],
        ),
    }
    return _assessment(role="planner", dimensions=dimensions)


def assess_reviewer_prediction(
    prediction: Any,
    oracle: Mapping[str, Any],
) -> dict[str, Any]:
    """Score one ReviewerProgram result against a frozen development oracle."""
    try:
        verdict = _prediction_value(prediction, "verdict")
        summary = _prediction_value(prediction, "summary")
        issues = _prediction_value(prediction, "issues")
        unrelated = _prediction_value(prediction, "unrelated_changes")
    except (AttributeError, KeyError, TypeError):
        return _assessment(
            role="reviewer",
            dimensions={name: False for name in REVIEWER_DIMENSIONS},
        )
    schema = (
        verdict in {"pass", "fail", "needs_attention"}
        and isinstance(summary, str)
        and bool(summary.strip())
        and _valid_string_list(issues, maximum=12)
        and _valid_string_list(unrelated, maximum=12)
    )
    if not schema:
        return _assessment(
            role="reviewer",
            dimensions={name: False for name in REVIEWER_DIMENSIONS},
        )
    dimensions = {
        "schema_valid": True,
        "verdict_match": verdict == oracle["verdict"],
        "required_issue_paths_found": _paths_found(
            issues, oracle["required_issue_paths"]
        ),
        "required_unrelated_paths_found": _paths_found(
            unrelated, oracle["required_unrelated_paths"]
        ),
        "forbidden_issue_paths_absent": _paths_absent(
            issues, oracle["forbidden_issue_paths"]
        ),
    }
    return _assessment(role="reviewer", dimensions=dimensions)


def _default_planner_runner(*, lm: Any, **inputs: Any) -> Any:
    from runtime.dspy_programs.planner import run_planner_program

    return run_planner_program(lm=lm, **inputs)


def _default_reviewer_runner(*, lm: Any, **inputs: Any) -> Any:
    from runtime.dspy_programs.reviewer import run_reviewer_program

    return run_reviewer_program(lm=lm, **inputs)


def run_case(
    *,
    case: RealTaskCase,
    attempt: int,
    route: str,
    lm: Any,
    runner: Callable[..., Any],
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run one real development case and retain only bounded score evidence."""
    started = clock()
    try:
        prediction = runner(lm=lm, **dict(case.inputs))
    except Exception:  # DSPy/provider exception types vary by installed version.
        dimensions = {
            name: False
            for name in (
                PLANNER_DIMENSIONS if case.role == "planner" else REVIEWER_DIMENSIONS
            )
        }
        return {
            "case_id": case.case_id,
            "case_hash": case.case_hash,
            "case_class": case.case_class,
            "difficulty": case.difficulty,
            "pattern_group": case.pattern_group,
            "role": case.role,
            "attempt": attempt,
            "route": route,
            "adapter_success": False,
            "dimensions": dimensions,
            "case_success": False,
            "score": 0.0,
            "failure_codes": ["adapter_error"],
            "prompt_tokens": None,
            "completion_tokens": None,
            "latency_seconds": max(0.0, clock() - started),
        }
    assessment = (
        assess_planner_prediction(prediction, case.oracle)
        if case.role == "planner"
        else assess_reviewer_prediction(prediction, case.oracle)
    )
    prompt_tokens, completion_tokens = _prediction_usage(prediction, route)
    return {
        "case_id": case.case_id,
        "case_hash": case.case_hash,
        "case_class": case.case_class,
        "difficulty": case.difficulty,
        "pattern_group": case.pattern_group,
        "role": case.role,
        "attempt": attempt,
        "route": route,
        "adapter_success": True,
        **assessment,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_seconds": max(0.0, clock() - started),
    }


def _mean_known(records: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [record[field] for record in records if record[field] is not None]
    return None if not values else sum(values) / len(values)


def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    failures: dict[str, int] = {}
    for record in records:
        for failure in record["failure_codes"]:
            failures[failure] = failures.get(failure, 0) + 1
    return {
        "cases": len(records),
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
        "mean_score": sum(float(record["score"]) for record in records) / len(records),
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
    """Return per-role, per-class, and overall development metrics."""
    by_role = {
        role: _metrics([record for record in records if record["role"] == role])
        for role in ROLES
    }
    classes = sorted({str(record["case_class"]) for record in records})
    by_class = {
        case_class: _metrics(
            [record for record in records if record["case_class"] == case_class]
        )
        for case_class in classes
    }
    return {
        "overall": _metrics(records),
        "by_role": by_role,
        "by_class": by_class,
    }


def collect_report(
    *,
    protocol: Protocol,
    suite: CaseSuite,
    subject_name: str,
    environment_id: str,
    implementation_commit: str,
    service_identity: Mapping[str, Any],
    prompt_state: Mapping[str, Mapping[str, str] | None],
    lm_factory: Callable[[str], Any] = build_dspy_lm,
    planner_runner: Callable[..., Any] = _default_planner_runner,
    reviewer_runner: Callable[..., Any] = _default_reviewer_runner,
) -> dict[str, Any]:
    """Collect one subject over every frozen development case exactly once."""
    if subject_name not in SUBJECTS:
        raise RealTaskDevelopmentError(f"Unsupported subject: {subject_name}")
    if suite.suite_id != protocol.suite_id or suite.suite_hash != protocol.suite_sha256:
        raise RealTaskDevelopmentError("Development suite does not match protocol")
    subject = protocol.subjects[subject_name]
    normalized_prompt_state = _normalize_prompt_state(prompt_state, "prompt_state")
    lms = {role: lm_factory(subject.routes[role]) for role in ROLES}
    runners = {"planner": planner_runner, "reviewer": reviewer_runner}
    records = [
        run_case(
            case=case,
            attempt=attempt,
            route=subject.routes[case.role],
            lm=lms[case.role],
            runner=runners[case.role],
        )
        for case in suite.cases
        for attempt in range(1, protocol.attempts_per_case + 1)
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "collection_kind": "real-task-development-v1",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "subject": subject_name,
        "corpus_id": suite.corpus_id,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "environment_id": _string(environment_id, "environment_id"),
        "implementation_commit": _string(
            implementation_commit, "implementation_commit"
        ),
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "routes": dict(subject.routes),
        "route_profiles": {role: dict(subject.route_profiles[role]) for role in ROLES},
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
    _exact_keys(
        data,
        {
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
        },
        name,
    )
    expected_identity = {
        "case_id": case.case_id,
        "case_hash": case.case_hash,
        "case_class": case.case_class,
        "difficulty": case.difficulty,
        "pattern_group": case.pattern_group,
        "role": case.role,
        "attempt": attempt_number,
        "route": route,
    }
    for field, expected in expected_identity.items():
        if data[field] != expected:
            raise RealTaskDevelopmentError(f"{name}.{field} does not match")
    adapter_success = _boolean(data["adapter_success"], f"{name}.adapter_success")
    dimension_names = (
        PLANNER_DIMENSIONS if case.role == "planner" else REVIEWER_DIMENSIONS
    )
    raw_dimensions = _mapping(data["dimensions"], f"{name}.dimensions")
    _exact_keys(raw_dimensions, set(dimension_names), f"{name}.dimensions")
    dimensions = {
        field: _boolean(raw_dimensions[field], f"{name}.dimensions.{field}")
        for field in dimension_names
    }
    if not adapter_success and any(dimensions.values()):
        raise RealTaskDevelopmentError(
            f"{name} failed adapter cannot have passing dimensions"
        )
    case_success = _boolean(data["case_success"], f"{name}.case_success")
    if case_success != (adapter_success and all(dimensions.values())):
        raise RealTaskDevelopmentError(f"{name}.case_success is inconsistent")
    score = _number(data["score"], f"{name}.score")
    expected_score = sum(dimensions.values()) / len(dimensions)
    if not math.isclose(score, expected_score, abs_tol=1e-12):
        raise RealTaskDevelopmentError(f"{name}.score is inconsistent")
    failures = data["failure_codes"]
    if not isinstance(failures, list) or not all(
        isinstance(item, str) and item for item in failures
    ):
        raise RealTaskDevelopmentError(f"{name}.failure_codes must be strings")
    expected_failures = _expected_failures(
        adapter_success=adapter_success,
        dimensions=dimensions,
        role=case.role,
    )
    if failures != expected_failures:
        raise RealTaskDevelopmentError(f"{name}.failure_codes are inconsistent")
    for field in ("prompt_tokens", "completion_tokens"):
        token_value = data[field]
        if token_value is not None:
            _integer(token_value, f"{name}.{field}")
    _number(data["latency_seconds"], f"{name}.latency_seconds")
    return dict(data)


def validate_report(
    value: Any,
    *,
    protocol: Protocol,
    suite: CaseSuite,
) -> Mapping[str, Any]:
    """Validate hashes, identities, every case record, and derived summaries."""
    report = _mapping(value, "report")
    _exact_keys(
        report,
        {
            "schema_version",
            "collection_kind",
            "protocol_id",
            "protocol_sha256",
            "scoring_version",
            "subject",
            "corpus_id",
            "suite_id",
            "suite_sha256",
            "environment_id",
            "implementation_commit",
            "model_file",
            "llama_alias",
            "routes",
            "route_profiles",
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
    if report["schema_version"] != 1:
        raise RealTaskDevelopmentError("report.schema_version must be 1")
    if report["collection_kind"] != "real-task-development-v1":
        raise RealTaskDevelopmentError("report.collection_kind is unsupported")
    if report["protocol_id"] != protocol.protocol_id:
        raise RealTaskDevelopmentError("report.protocol_id does not match")
    if report["protocol_sha256"] != protocol.sha256:
        raise RealTaskDevelopmentError("report.protocol_sha256 does not match")
    if report["scoring_version"] != protocol.scoring_version:
        raise RealTaskDevelopmentError("report.scoring_version does not match")
    if (
        report["corpus_id"] != suite.corpus_id
        or report["suite_id"] != suite.suite_id
        or report["suite_sha256"] != suite.suite_hash
    ):
        raise RealTaskDevelopmentError("report suite identity does not match")
    if report["holdout_loaded"] is not False:
        raise RealTaskDevelopmentError("Development report must not load holdout")
    expected_hash = _string(report["collection_sha256"], "report.collection_sha256")
    unhashed = dict(report)
    del unhashed["collection_sha256"]
    if expected_hash != stable_hash(unhashed):
        raise RealTaskDevelopmentError("report.collection_sha256 is invalid")
    subject_name = _string(report["subject"], "report.subject")
    if subject_name not in SUBJECTS:
        raise RealTaskDevelopmentError("report.subject is unsupported")
    subject = protocol.subjects[subject_name]
    if report["model_file"] != subject.model_file:
        raise RealTaskDevelopmentError("report.model_file does not match protocol")
    if report["llama_alias"] != subject.llama_alias:
        raise RealTaskDevelopmentError("report.llama_alias does not match protocol")
    if dict(_mapping(report["routes"], "report.routes")) != dict(subject.routes):
        raise RealTaskDevelopmentError("report.routes do not match protocol")
    expected_profiles = {role: dict(subject.route_profiles[role]) for role in ROLES}
    actual_profiles = dict(_mapping(report["route_profiles"], "report.route_profiles"))
    if actual_profiles != expected_profiles:
        raise RealTaskDevelopmentError("report.route_profiles do not match protocol")
    _normalize_prompt_state(report["active_prompt_state"], "report.active_prompt_state")
    _string(report["environment_id"], "report.environment_id")
    commit = _string(report["implementation_commit"], "report.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise RealTaskDevelopmentError("report.implementation_commit must be full hash")
    timestamp = _string(report["collected_at_utc"], "report.collected_at_utc")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise RealTaskDevelopmentError("report.collected_at_utc is invalid") from exc
    if parsed_timestamp.tzinfo is None:
        raise RealTaskDevelopmentError("report.collected_at_utc must include timezone")
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
    if identity["model_file"] != subject.model_file:
        raise RealTaskDevelopmentError("report service model is inconsistent")
    if identity["llama_alias"] != subject.llama_alias:
        raise RealTaskDevelopmentError("report service alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise RealTaskDevelopmentError("report build_info must be a string")
    _integer(identity["configured_context_tokens"], "report configured context")
    _integer(identity["total_slots"], "report total slots", minimum=1)
    attempts = report["attempts"]
    if not isinstance(attempts, list):
        raise RealTaskDevelopmentError("report.attempts must be an array")
    expected_count = len(suite.cases) * protocol.attempts_per_case
    if len(attempts) != expected_count:
        raise RealTaskDevelopmentError(
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
                    route=subject.routes[case.role],
                )
            )
            offset += 1
    if report["summary"] != summarize(validated):
        raise RealTaskDevelopmentError("report.summary does not match attempts")
    if report["storage_policy"] != STORAGE_POLICY:
        raise RealTaskDevelopmentError("report.storage_policy is unsupported")
    return report


def default_output_path(subject: str) -> Path:
    """Return a non-overwriting timestamped development-report path."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{subject}-track-g-development-v1-{timestamp}.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    result.add_argument("--subject", choices=SUBJECTS, required=True)
    result.add_argument("--environment-id", required=True)
    result.add_argument("--output", type=Path)
    result.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    protocol = load_protocol(arguments.protocol)
    suite = load_development_suite(protocol)
    subject = protocol.subjects[arguments.subject]
    output = arguments.output or default_output_path(arguments.subject)
    prompt_state = active_prompt_snapshot()
    report = collect_report(
        protocol=protocol,
        suite=suite,
        subject_name=arguments.subject,
        environment_id=arguments.environment_id,
        implementation_commit=git_commit(),
        service_identity=inspect_service(
            client=HttpClient(),
            llama_base_url=arguments.llama_base_url,
            subject=subject,
        ),
        prompt_state=prompt_state,
    )
    if active_prompt_snapshot() != prompt_state:
        raise RealTaskDevelopmentError(
            "Active prompt state changed during development collection"
        )
    validate_report(report, protocol=protocol, suite=suite)
    write_report(output, report)
    print(
        json.dumps(
            {
                "output": str(output),
                "collection_sha256": report["collection_sha256"],
                "subject": report["subject"],
                "suite_sha256": report["suite_sha256"],
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RealTaskDevelopmentError,
        ReportWriteError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"real-task-development: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
