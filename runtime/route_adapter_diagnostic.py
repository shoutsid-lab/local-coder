"""Collect and compare planner/reviewer evidence through the real DSPy adapters."""

from __future__ import annotations

import argparse
import hashlib
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

from .dspy_lm import build_dspy_lm
from .route_contract_diagnostic import (
    DiagnosticError as RawDiagnosticError,
    HttpClient,
    inspect_service,
    write_report,
)
from .prompt_activation import PromptActivationError, read_active_prompt
from .route_profiles import get_route_profile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "qwythos-f3-adapter-contract-v1.json"
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "qualifications"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
ROLES = ("planner", "reviewer")
SUBJECTS = ("baseline", "candidate")
PROMPT_STATE_FIELDS = (
    "activation_id",
    "campaign_id",
    "build_id",
    "evaluation_id",
    "candidate_instruction_hash",
    "program_hash",
)
FAILURE_CODES = frozenset(
    {
        "adapter_error",
        "schema_mismatch",
        "task_semantics_mismatch",
    }
)


class AdapterDiagnosticError(ValueError):
    """Raised when adapter-level diagnostic input or evidence is invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterDiagnosticError(f"{name} must be a JSON object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterDiagnosticError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise AdapterDiagnosticError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise AdapterDiagnosticError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AdapterDiagnosticError(f"{name} must be a boolean")
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
    raise AdapterDiagnosticError(f"{name} has invalid fields: {'; '.join(details)}")


@dataclass(frozen=True)
class Subject:
    """One model and the logical routes used by the shared role adapters."""

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
        _exact_keys(routes, set(ROLES), f"{name}.routes")
        route_profiles = _mapping(data["route_profiles"], f"{name}.route_profiles")
        _exact_keys(route_profiles, set(ROLES), f"{name}.route_profiles")
        parsed_routes = {
            role: _string(routes[role], f"{name}.routes.{role}") for role in ROLES
        }
        parsed_profiles = {
            role: dict(
                _mapping(
                    route_profiles[role],
                    f"{name}.route_profiles.{role}",
                )
            )
            for role in ROLES
        }
        for role, route in parsed_routes.items():
            if parsed_profiles[role] != route_profile_snapshot(route):
                raise AdapterDiagnosticError(
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
    """Versioned adapter-level comparison protocol."""

    raw: Mapping[str, Any]
    protocol_id: str
    fixture_version: str
    attempts_per_role: int
    subjects: Mapping[str, Subject]

    @property
    def sha256(self) -> str:
        return _sha256(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "Protocol":
        data = _mapping(value, "protocol")
        _exact_keys(
            data,
            {
                "schema_version",
                "protocol_id",
                "fixture_version",
                "attempts_per_role",
                "subjects",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise AdapterDiagnosticError("protocol.schema_version must be 1")
        subjects = _mapping(data["subjects"], "protocol.subjects")
        _exact_keys(subjects, set(SUBJECTS), "protocol.subjects")
        parsed_subjects = {
            name: Subject.from_mapping(subjects[name], f"protocol.subjects.{name}")
            for name in SUBJECTS
        }
        return cls(
            raw=dict(data),
            protocol_id=_string(data["protocol_id"], "protocol.protocol_id"),
            fixture_version=_string(
                data["fixture_version"], "protocol.fixture_version"
            ),
            attempts_per_role=_integer(
                data["attempts_per_role"],
                "protocol.attempts_per_role",
                minimum=1,
            ),
            subjects=parsed_subjects,
        )


def load_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdapterDiagnosticError(f"Unable to read {name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AdapterDiagnosticError(f"Invalid JSON in {name}: {path}") from exc


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> Protocol:
    return Protocol.from_mapping(load_json(path, "adapter protocol"))


def git_commit(root: Path = ROOT) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise AdapterDiagnosticError(
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
        raise AdapterDiagnosticError("Unable to resolve a full implementation commit")
    return commit


def planner_fixture() -> dict[str, Any]:
    """Return the fixed PlannerProgram inputs."""
    return {
        "task": (
            "Change only the local-reason timeout from 300 to 240 seconds in "
            "runtime/route_profiles.py."
        ),
        "delegated_task": (
            "Produce one implementation-ready atomic plan for the timeout change."
        ),
        "repository_evidence": [
            "runtime/route_profiles.py defines local-reason timeout_seconds=300.",
            "The required value is 240 seconds.",
            "Only runtime/route_profiles.py may be edited.",
        ],
    }


def reviewer_fixture() -> dict[str, Any]:
    """Return the fixed ReviewerProgram inputs."""
    return {
        "task": (
            "Change only the local-reason timeout from 300 to 240 seconds in "
            "runtime/route_profiles.py."
        ),
        "changed_files": ["runtime/route_profiles.py"],
        "verification_passed": False,
        "verification_output": (
            "FAILED test_local_reason_timeout: expected 240, found 300"
        ),
        "diff": (
            "diff --git a/runtime/route_profiles.py "
            "b/runtime/route_profiles.py\n"
            "--- a/runtime/route_profiles.py\n"
            "+++ b/runtime/route_profiles.py\n"
            "@@\n"
            "-        timeout_seconds=300,\n"
            "+        timeout_seconds=300,\n"
        ),
    }


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


def assess_planner_prediction(prediction: Any) -> dict[str, Any]:
    """Assess typed planner fields without retaining generated text."""
    try:
        instruction = _prediction_value(prediction, "instruction")
        editable_files = _prediction_value(prediction, "editable_files")
        acceptance_criteria = _prediction_value(prediction, "acceptance_criteria")
        depends_on = _prediction_value(prediction, "depends_on")
    except (AttributeError, KeyError, TypeError):
        return {
            "schema_valid": False,
            "task_semantics_valid": False,
            "contract_failures": ["schema_mismatch"],
        }
    schema = (
        isinstance(instruction, str)
        and bool(instruction.strip())
        and _valid_string_list(editable_files, minimum=1, maximum=2)
        and _valid_string_list(acceptance_criteria, minimum=1, maximum=6)
        and _valid_string_list(depends_on, maximum=6)
    )
    semantics = False
    if schema:
        criteria = " ".join(acceptance_criteria).lower()
        instruction_text = instruction.lower()
        semantics = (
            editable_files == ["runtime/route_profiles.py"]
            and depends_on == []
            and "240" in f"{instruction_text} {criteria}"
            and any(
                term in f"{instruction_text} {criteria}"
                for term in ("timeout", "local-reason")
            )
        )
    failures: list[str] = []
    if not schema:
        failures.append("schema_mismatch")
    elif not semantics:
        failures.append("task_semantics_mismatch")
    return {
        "schema_valid": bool(schema),
        "task_semantics_valid": bool(semantics),
        "contract_failures": failures,
    }


def assess_reviewer_prediction(prediction: Any) -> dict[str, Any]:
    """Assess typed reviewer fields without retaining generated text."""
    try:
        verdict = _prediction_value(prediction, "verdict")
        summary = _prediction_value(prediction, "summary")
        issues = _prediction_value(prediction, "issues")
        unrelated_changes = _prediction_value(prediction, "unrelated_changes")
    except (AttributeError, KeyError, TypeError):
        return {
            "schema_valid": False,
            "task_semantics_valid": False,
            "contract_failures": ["schema_mismatch"],
        }
    schema = (
        verdict in {"pass", "fail", "needs_attention"}
        and isinstance(summary, str)
        and bool(summary.strip())
        and _valid_string_list(issues, maximum=12)
        and _valid_string_list(unrelated_changes, maximum=12)
    )
    semantics = bool(
        schema and verdict == "fail" and issues and unrelated_changes == []
    )
    failures: list[str] = []
    if not schema:
        failures.append("schema_mismatch")
    elif not semantics:
        failures.append("task_semantics_mismatch")
    return {
        "schema_valid": bool(schema),
        "task_semantics_valid": bool(semantics),
        "contract_failures": failures,
    }


def _default_planner_runner(*, lm: Any, **inputs: Any) -> Any:
    from .dspy_programs.planner import run_planner_program

    return run_planner_program(lm=lm, **inputs)


def _default_reviewer_runner(*, lm: Any, **inputs: Any) -> Any:
    from .dspy_programs.reviewer import run_reviewer_program

    return run_reviewer_program(lm=lm, **inputs)


def run_attempt(
    *,
    role: str,
    index: int,
    route: str,
    lm: Any,
    runner: Callable[..., Any],
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run one shared-adapter attempt and retain only bounded classifications."""
    fixture = planner_fixture() if role == "planner" else reviewer_fixture()
    started = clock()
    try:
        prediction = runner(lm=lm, **fixture)
    except Exception:
        return {
            "attempt_id": f"{role}-{index}",
            "role": role,
            "route": route,
            "program": "PlannerProgram" if role == "planner" else "ReviewerProgram",
            "adapter": "JSONAdapter",
            "adapter_success": False,
            "schema_valid": False,
            "task_semantics_valid": False,
            "contract_failures": ["adapter_error"],
            "prompt_tokens": None,
            "completion_tokens": None,
            "latency_seconds": max(0.0, clock() - started),
        }
    assessment = (
        assess_planner_prediction(prediction)
        if role == "planner"
        else assess_reviewer_prediction(prediction)
    )
    prompt_tokens, completion_tokens = _prediction_usage(prediction, route)
    return {
        "attempt_id": f"{role}-{index}",
        "role": role,
        "route": route,
        "program": "PlannerProgram" if role == "planner" else "ReviewerProgram",
        "adapter": "JSONAdapter",
        "adapter_success": True,
        **assessment,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_seconds": max(0.0, clock() - started),
    }


def _rate(records: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(bool(record[field]) for record in records) / len(records)


def _mean_optional_int(
    records: Sequence[Mapping[str, Any]], field: str
) -> float | None:
    values = [record[field] for record in records if record[field] is not None]
    return None if not values else sum(values) / len(values)


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize adapter success, typed schema, task semantics, and cost."""
    result: dict[str, Any] = {}
    for role in ROLES:
        role_records = [record for record in records if record["role"] == role]
        failures: dict[str, int] = {}
        for record in role_records:
            for failure in record["contract_failures"]:
                failures[failure] = failures.get(failure, 0) + 1
        result[role] = {
            "attempts": len(role_records),
            "failure_counts": failures,
            "adapter_success_rate": _rate(role_records, "adapter_success"),
            "schema_rate": _rate(role_records, "schema_valid"),
            "task_semantics_rate": _rate(role_records, "task_semantics_valid"),
            "mean_prompt_tokens": _mean_optional_int(role_records, "prompt_tokens"),
            "mean_completion_tokens": _mean_optional_int(
                role_records, "completion_tokens"
            ),
            "mean_latency_seconds": sum(
                float(record["latency_seconds"]) for record in role_records
            )
            / len(role_records),
        }
    return result


def route_profile_snapshot(route: str) -> dict[str, Any]:
    """Bind evidence to the exact runtime generation profile."""
    try:
        return asdict(get_route_profile(route))
    except KeyError as exc:
        raise AdapterDiagnosticError(f"Unknown adapter route: {route}") from exc


def active_prompt_snapshot() -> dict[str, dict[str, Any] | None]:
    """Return bounded active-prompt lineage without retaining prompt text or paths."""
    snapshot: dict[str, dict[str, Any] | None] = {}
    for role in ROLES:
        try:
            pointer = read_active_prompt(role)
        except PromptActivationError as exc:
            raise AdapterDiagnosticError(
                f"Unable to bind active {role} prompt state"
            ) from exc
        if pointer is None:
            snapshot[role] = None
            continue
        state = {field: pointer.get(field) for field in PROMPT_STATE_FIELDS}
        if any(not isinstance(value, str) or not value for value in state.values()):
            raise AdapterDiagnosticError(f"Active {role} prompt lineage is incomplete")
        snapshot[role] = state
    return snapshot


def _normalize_prompt_state(
    value: Any,
    name: str,
) -> dict[str, dict[str, Any] | None]:
    state_by_role = _mapping(value, name)
    _exact_keys(state_by_role, set(ROLES), name)
    normalized: dict[str, dict[str, Any] | None] = {}
    for role in ROLES:
        state = state_by_role[role]
        if state is None:
            normalized[role] = None
            continue
        state_mapping = _mapping(state, f"{name}.{role}")
        _exact_keys(
            state_mapping,
            set(PROMPT_STATE_FIELDS),
            f"{name}.{role}",
        )
        normalized[role] = {
            field: _string(
                state_mapping[field],
                f"{name}.{role}.{field}",
            )
            for field in PROMPT_STATE_FIELDS
        }
    return normalized


def collect_report(
    *,
    protocol: Protocol,
    subject_name: str,
    environment_id: str,
    implementation_commit: str,
    service_identity: Mapping[str, Any],
    prompt_state: Mapping[str, Mapping[str, Any] | None],
    lm_factory: Callable[[str], Any] = build_dspy_lm,
    planner_runner: Callable[..., Any] = _default_planner_runner,
    reviewer_runner: Callable[..., Any] = _default_reviewer_runner,
) -> dict[str, Any]:
    """Collect one model subject through the same typed DSPy role adapters."""
    if subject_name not in SUBJECTS:
        raise AdapterDiagnosticError(f"Unsupported subject: {subject_name}")
    subject = protocol.subjects[subject_name]
    prompt_state_mapping = _normalize_prompt_state(prompt_state, "prompt_state")
    records: list[dict[str, Any]] = []
    for role, runner in (
        ("planner", planner_runner),
        ("reviewer", reviewer_runner),
    ):
        route = subject.routes[role]
        lm = lm_factory(route)
        records.extend(
            run_attempt(
                role=role,
                index=index,
                route=route,
                lm=lm,
                runner=runner,
            )
            for index in range(1, protocol.attempts_per_role + 1)
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "collection_kind": "route-adapter-diagnostic-v1",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "fixture_version": protocol.fixture_version,
        "subject": subject_name,
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "routes": dict(subject.routes),
        "route_profiles": {role: dict(subject.route_profiles[role]) for role in ROLES},
        "active_prompt_state": {
            role: (
                None
                if prompt_state_mapping[role] is None
                else dict(prompt_state_mapping[role])
            )
            for role in ROLES
        },
        "environment_id": _string(environment_id, "environment_id"),
        "implementation_commit": _string(
            implementation_commit, "implementation_commit"
        ),
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "service_identity": dict(service_identity),
        "attempts": records,
        "summary": summarize(records),
        "storage_policy": (
            "no fixture prompt, generated field text, final-answer, or "
            "reasoning text retained"
        ),
    }
    report["collection_sha256"] = _sha256(report)
    return report


def _validate_optional_token(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _validate_attempt(
    value: Any,
    *,
    subject: Subject,
    role: str,
    index: int,
) -> dict[str, Any]:
    attempt = _mapping(value, f"report.attempts[{role}-{index}]")
    _exact_keys(
        attempt,
        {
            "attempt_id",
            "role",
            "route",
            "program",
            "adapter",
            "adapter_success",
            "schema_valid",
            "task_semantics_valid",
            "contract_failures",
            "prompt_tokens",
            "completion_tokens",
            "latency_seconds",
        },
        f"report.attempts[{role}-{index}]",
    )
    if attempt["attempt_id"] != f"{role}-{index}":
        raise AdapterDiagnosticError("report attempt order or identifier is invalid")
    if attempt["role"] != role or attempt["route"] != subject.routes[role]:
        raise AdapterDiagnosticError("report attempt role or route is inconsistent")
    expected_program = "PlannerProgram" if role == "planner" else "ReviewerProgram"
    if attempt["program"] != expected_program or attempt["adapter"] != "JSONAdapter":
        raise AdapterDiagnosticError("report attempt adapter identity is inconsistent")
    adapter_success = _boolean(
        attempt["adapter_success"], f"report.attempts[{role}-{index}].adapter_success"
    )
    schema_valid = _boolean(
        attempt["schema_valid"], f"report.attempts[{role}-{index}].schema_valid"
    )
    semantics_valid = _boolean(
        attempt["task_semantics_valid"],
        f"report.attempts[{role}-{index}].task_semantics_valid",
    )
    failures = attempt["contract_failures"]
    if (
        not isinstance(failures, list)
        or any(failure not in FAILURE_CODES for failure in failures)
        or len(failures) != len(set(failures))
    ):
        raise AdapterDiagnosticError("report attempt failures are invalid")
    expected_failures: list[str] = []
    if not adapter_success:
        expected_failures = ["adapter_error"]
    elif not schema_valid:
        expected_failures = ["schema_mismatch"]
    elif not semantics_valid:
        expected_failures = ["task_semantics_mismatch"]
    if failures != expected_failures:
        raise AdapterDiagnosticError("report attempt failures are inconsistent")
    if semantics_valid and not schema_valid:
        raise AdapterDiagnosticError("task semantics cannot pass an invalid schema")
    if schema_valid and not adapter_success:
        raise AdapterDiagnosticError("schema cannot pass when the adapter failed")
    return {
        "attempt_id": attempt["attempt_id"],
        "role": role,
        "route": attempt["route"],
        "program": attempt["program"],
        "adapter": attempt["adapter"],
        "adapter_success": adapter_success,
        "schema_valid": schema_valid,
        "task_semantics_valid": semantics_valid,
        "contract_failures": list(failures),
        "prompt_tokens": _validate_optional_token(
            attempt["prompt_tokens"],
            f"report.attempts[{role}-{index}].prompt_tokens",
        ),
        "completion_tokens": _validate_optional_token(
            attempt["completion_tokens"],
            f"report.attempts[{role}-{index}].completion_tokens",
        ),
        "latency_seconds": _number(
            attempt["latency_seconds"],
            f"report.attempts[{role}-{index}].latency_seconds",
        ),
    }


def validate_report(value: Any, protocol: Protocol) -> Mapping[str, Any]:
    """Validate report binding, attempt classifications, and derived summary."""
    report = _mapping(value, "report")
    _exact_keys(
        report,
        {
            "schema_version",
            "collection_kind",
            "protocol_id",
            "protocol_sha256",
            "fixture_version",
            "subject",
            "model_file",
            "llama_alias",
            "routes",
            "route_profiles",
            "active_prompt_state",
            "environment_id",
            "implementation_commit",
            "collected_at_utc",
            "service_identity",
            "attempts",
            "summary",
            "storage_policy",
            "collection_sha256",
        },
        "report",
    )
    if report["schema_version"] != 1:
        raise AdapterDiagnosticError("report.schema_version must be 1")
    if report["collection_kind"] != "route-adapter-diagnostic-v1":
        raise AdapterDiagnosticError("report.collection_kind is unsupported")
    if report["protocol_id"] != protocol.protocol_id:
        raise AdapterDiagnosticError("report.protocol_id does not match the protocol")
    if report["protocol_sha256"] != protocol.sha256:
        raise AdapterDiagnosticError("report.protocol_sha256 does not match")
    if report["fixture_version"] != protocol.fixture_version:
        raise AdapterDiagnosticError("report.fixture_version does not match")
    expected_hash = _string(report["collection_sha256"], "report.collection_sha256")
    unhashed = dict(report)
    del unhashed["collection_sha256"]
    if expected_hash != _sha256(unhashed):
        raise AdapterDiagnosticError("report.collection_sha256 is invalid")
    subject_name = _string(report["subject"], "report.subject")
    if subject_name not in SUBJECTS:
        raise AdapterDiagnosticError("report.subject is unsupported")
    subject = protocol.subjects[subject_name]
    if report["model_file"] != subject.model_file:
        raise AdapterDiagnosticError("report.model_file does not match the protocol")
    if report["llama_alias"] != subject.llama_alias:
        raise AdapterDiagnosticError("report.llama_alias does not match the protocol")
    if dict(_mapping(report["routes"], "report.routes")) != dict(subject.routes):
        raise AdapterDiagnosticError("report.routes do not match the protocol")
    expected_profiles = {role: dict(subject.route_profiles[role]) for role in ROLES}
    actual_profiles = dict(_mapping(report["route_profiles"], "report.route_profiles"))
    if actual_profiles != expected_profiles:
        raise AdapterDiagnosticError("report.route_profiles do not match the protocol")
    _normalize_prompt_state(
        report["active_prompt_state"],
        "report.active_prompt_state",
    )
    _string(report["environment_id"], "report.environment_id")
    commit = _string(report["implementation_commit"], "report.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise AdapterDiagnosticError("report.implementation_commit must be a full hash")
    timestamp = _string(report["collected_at_utc"], "report.collected_at_utc")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise AdapterDiagnosticError("report.collected_at_utc is invalid") from exc
    if parsed_timestamp.tzinfo is None:
        raise AdapterDiagnosticError("report.collected_at_utc must include timezone")
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
        raise AdapterDiagnosticError("report.service_identity model is inconsistent")
    if identity["llama_alias"] != subject.llama_alias:
        raise AdapterDiagnosticError("report.service_identity alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise AdapterDiagnosticError(
            "report.service_identity.build_info must be a string"
        )
    _integer(
        identity["configured_context_tokens"],
        "report.service_identity.configured_context_tokens",
    )
    _integer(
        identity["total_slots"],
        "report.service_identity.total_slots",
        minimum=1,
    )
    attempts = report["attempts"]
    if not isinstance(attempts, list):
        raise AdapterDiagnosticError("report.attempts must be an array")
    expected_count = protocol.attempts_per_role * len(ROLES)
    if len(attempts) != expected_count:
        raise AdapterDiagnosticError(
            f"report requires {expected_count} attempts, found {len(attempts)}"
        )
    validated = [
        _validate_attempt(
            attempts[(role_index * protocol.attempts_per_role) + index - 1],
            subject=subject,
            role=role,
            index=index,
        )
        for role_index, role in enumerate(ROLES)
        for index in range(1, protocol.attempts_per_role + 1)
    ]
    if report["summary"] != summarize(validated):
        raise AdapterDiagnosticError("report.summary does not match attempts")
    if report["storage_policy"] != (
        "no fixture prompt, generated field text, final-answer, or "
        "reasoning text retained"
    ):
        raise AdapterDiagnosticError("report.storage_policy is unsupported")
    return report


def compare_reports(
    *,
    protocol: Protocol,
    baseline_value: Any,
    candidate_value: Any,
) -> dict[str, Any]:
    """Compare adapter-level reports without making a qualification decision."""
    baseline = validate_report(baseline_value, protocol)
    candidate = validate_report(candidate_value, protocol)
    if baseline["subject"] != "baseline":
        raise AdapterDiagnosticError("The baseline report is not a baseline subject")
    if candidate["subject"] != "candidate":
        raise AdapterDiagnosticError("The candidate report is not a candidate subject")
    for field in (
        "fixture_version",
        "environment_id",
        "implementation_commit",
        "active_prompt_state",
    ):
        if baseline[field] != candidate[field]:
            raise AdapterDiagnosticError(f"Reports use different {field}")
    for field in ("build_info", "configured_context_tokens", "total_slots"):
        if baseline["service_identity"][field] != candidate["service_identity"][field]:
            raise AdapterDiagnosticError(f"Reports use different llama.cpp {field}")
    comparison: dict[str, Any] = {}
    for role in ROLES:
        baseline_metrics = baseline["summary"][role]
        candidate_metrics = candidate["summary"][role]
        deltas: dict[str, float | None] = {}
        for field in (
            "adapter_success_rate",
            "schema_rate",
            "task_semantics_rate",
            "mean_prompt_tokens",
            "mean_completion_tokens",
            "mean_latency_seconds",
        ):
            baseline_field = baseline_metrics[field]
            candidate_field = candidate_metrics[field]
            deltas[field] = (
                None
                if baseline_field is None or candidate_field is None
                else float(candidate_field) - float(baseline_field)
            )
        comparison[role] = {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "candidate_minus_baseline": deltas,
        }
    return {
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "fixture_version": protocol.fixture_version,
        "environment_id": baseline["environment_id"],
        "implementation_commit": baseline["implementation_commit"],
        "baseline_collection_sha256": baseline["collection_sha256"],
        "candidate_collection_sha256": candidate["collection_sha256"],
        "comparison": comparison,
        "qualification_claim": None,
    }


def default_output_path(subject: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{subject}-f3-adapter-v1-{timestamp}.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    subparsers = result.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect one adapter report.")
    collect.add_argument("--subject", choices=SUBJECTS, required=True)
    collect.add_argument("--environment-id", required=True)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)

    compare = subparsers.add_parser("compare", help="Compare adapter reports.")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    protocol = load_protocol(arguments.protocol)
    if arguments.command == "collect":
        subject = protocol.subjects[arguments.subject]
        output = arguments.output or default_output_path(arguments.subject)
        prompt_state = active_prompt_snapshot()
        report = collect_report(
            protocol=protocol,
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
            raise AdapterDiagnosticError(
                "Active prompt state changed during collection"
            )
        write_report(output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "collection_sha256": report["collection_sha256"],
                    "subject": report["subject"],
                    "summary": report["summary"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    comparison = compare_reports(
        protocol=protocol,
        baseline_value=load_json(arguments.baseline, "baseline adapter report"),
        candidate_value=load_json(arguments.candidate, "candidate adapter report"),
    )
    if arguments.output:
        write_report(arguments.output, comparison)
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AdapterDiagnosticError,
        RawDiagnosticError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"route-adapter-diagnostic: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
