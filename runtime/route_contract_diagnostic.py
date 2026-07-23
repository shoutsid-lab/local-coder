"""Collect and compare focused planner/reviewer contract diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model_response import (
    EMPTY_COMPLETION,
    MALFORMED_FINAL,
    PROVIDER_ERROR,
    REASONING_ONLY_TRUNCATED,
    ROUTE_OK,
    TOOL_CALL_OK,
    normalize_model_response,
    normalize_provider_error,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "qwythos-f3-focused-contract-v2.json"
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "qualifications"
DEFAULT_LITELLM_BASE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
SUITES = ("exact", "planner", "reviewer")
SUBJECTS = ("baseline", "candidate")
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
FAILURE_CODES = frozenset(
    {
        "response_failure",
        "empty_final",
        "malformed_tool_call",
        "exact_mismatch",
        "non_json_final",
        "schema_mismatch",
        "task_semantics_mismatch",
    }
)


class DiagnosticError(ValueError):
    """Raised when focused diagnostic input or runtime state is invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DiagnosticError(f"{name} must be a JSON object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosticError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise DiagnosticError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise DiagnosticError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise DiagnosticError(f"{name} must be a boolean")
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
    raise DiagnosticError(f"{name} has invalid fields: {'; '.join(details)}")


@dataclass(frozen=True)
class Profile:
    """One bounded operational profile used by the comparison protocol."""

    reasoning_mode: str
    max_tokens: int
    reasoning_tokens: int
    temperature: float
    top_p: float
    top_k: int | None
    repetition_penalty: float | None
    timeout_seconds: int

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "Profile":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {
                "reasoning_mode",
                "max_tokens",
                "reasoning_tokens",
                "temperature",
                "top_p",
                "top_k",
                "repetition_penalty",
                "timeout_seconds",
            },
            name,
        )
        reasoning_mode = _string(data["reasoning_mode"], f"{name}.reasoning_mode")
        if reasoning_mode not in {"off", "on"}:
            raise DiagnosticError(f"{name}.reasoning_mode must be off or on")
        max_tokens = _integer(data["max_tokens"], f"{name}.max_tokens", minimum=1)
        reasoning_tokens = _integer(
            data["reasoning_tokens"], f"{name}.reasoning_tokens"
        )
        if reasoning_mode == "off" and reasoning_tokens:
            raise DiagnosticError(
                f"{name}.reasoning_tokens must be zero when reasoning is off"
            )
        if reasoning_tokens > max_tokens:
            raise DiagnosticError(f"{name}.reasoning_tokens exceeds max_tokens")
        temperature = _number(data["temperature"], f"{name}.temperature")
        if temperature > 2:
            raise DiagnosticError(f"{name}.temperature must be <= 2")
        top_p = _number(data["top_p"], f"{name}.top_p")
        if not 0 < top_p <= 1:
            raise DiagnosticError(f"{name}.top_p must be in (0, 1]")
        top_k = data["top_k"]
        if top_k is not None:
            top_k = _integer(top_k, f"{name}.top_k", minimum=1)
        repetition_penalty = data["repetition_penalty"]
        if repetition_penalty is not None:
            repetition_penalty = _number(
                repetition_penalty, f"{name}.repetition_penalty", minimum=0.000001
            )
        return cls(
            reasoning_mode=reasoning_mode,
            max_tokens=max_tokens,
            reasoning_tokens=reasoning_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            repetition_penalty=repetition_penalty,
            timeout_seconds=_integer(
                data["timeout_seconds"], f"{name}.timeout_seconds", minimum=1
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "reasoning_mode": self.reasoning_mode,
            "max_tokens": self.max_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class Subject:
    """One model and its intended operational planner/reviewer routes."""

    model_file: str
    llama_alias: str
    routes: Mapping[str, str]
    profiles: Mapping[str, Profile]

    @classmethod
    def from_mapping(cls, value: Any, name: str) -> "Subject":
        data = _mapping(value, name)
        _exact_keys(
            data,
            {"model_file", "llama_alias", "routes", "profiles"},
            name,
        )
        routes = _mapping(data["routes"], f"{name}.routes")
        _exact_keys(routes, set(SUITES), f"{name}.routes")
        profiles = _mapping(data["profiles"], f"{name}.profiles")
        _exact_keys(profiles, {"planner", "reviewer"}, f"{name}.profiles")
        return cls(
            model_file=_string(data["model_file"], f"{name}.model_file"),
            llama_alias=_string(data["llama_alias"], f"{name}.llama_alias"),
            routes={
                suite: _string(routes[suite], f"{name}.routes.{suite}")
                for suite in SUITES
            },
            profiles={
                role: Profile.from_mapping(profiles[role], f"{name}.profiles.{role}")
                for role in ("planner", "reviewer")
            },
        )


@dataclass(frozen=True)
class Protocol:
    """Versioned focused-contract comparison protocol."""

    raw: Mapping[str, Any]
    protocol_id: str
    fixture_version: str
    attempts_per_suite: int
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
                "attempts_per_suite",
                "subjects",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise DiagnosticError("protocol.schema_version must be 1")
        subjects = _mapping(data["subjects"], "protocol.subjects")
        _exact_keys(subjects, set(SUBJECTS), "protocol.subjects")
        return cls(
            raw=dict(data),
            protocol_id=_string(data["protocol_id"], "protocol.protocol_id"),
            fixture_version=_string(
                data["fixture_version"], "protocol.fixture_version"
            ),
            attempts_per_suite=_integer(
                data["attempts_per_suite"],
                "protocol.attempts_per_suite",
                minimum=1,
            ),
            subjects={
                subject: Subject.from_mapping(
                    subjects[subject], f"protocol.subjects.{subject}"
                )
                for subject in SUBJECTS
            },
        )


def load_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DiagnosticError(f"Unable to read {name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"Invalid JSON in {name}: {path}") from exc


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> Protocol:
    return Protocol.from_mapping(load_json(path, "protocol"))


class HttpClient:
    """Small JSON/text client with bounded requests and normalized failures."""

    def get_json(self, url: str, *, timeout: float = 10.0) -> Mapping[str, Any]:
        return self._request_json(url, method="GET", timeout=timeout)

    def get_text(self, url: str, *, timeout: float = 10.0) -> str:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiagnosticError(f"GET failed for {url}") from exc

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        return self._request_json(
            url,
            method="POST",
            payload=payload,
            timeout=timeout,
        )

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        timeout: float,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiagnosticError(f"{method} failed for {url}") from exc
        except json.JSONDecodeError as exc:
            raise DiagnosticError(f"{method} returned invalid JSON for {url}") from exc
        return _mapping(decoded, f"response from {url}")


def _url(base: str, suffix: str) -> str:
    return base.rstrip("/") + "/" + suffix.lstrip("/")


def git_commit(root: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise DiagnosticError("Collection requires a committed, clean working tree")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(commit) != 40:
        raise DiagnosticError("Unable to resolve a full implementation commit")
    return commit


def inspect_service(
    *,
    client: HttpClient,
    llama_base_url: str,
    subject: Subject,
) -> dict[str, Any]:
    health = client.get_json(_url(llama_base_url, "/health"))
    if health.get("status") not in {"ok", "ready"}:
        raise DiagnosticError("llama.cpp service is not ready")
    props = client.get_json(_url(llama_base_url, "/props"))
    model_path = _string(props.get("model_path"), "llama.props.model_path")
    model_file = Path(model_path).name
    if model_file != subject.model_file:
        raise DiagnosticError(
            "Active model does not match the selected subject: "
            f"expected {subject.model_file}, found {model_file}"
        )
    models = client.get_json(_url(llama_base_url, "/v1/models"))
    aliases = {
        str(item.get("id"))
        for item in models.get("data", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    if subject.llama_alias not in aliases:
        raise DiagnosticError(
            "Active llama.cpp alias does not match the selected subject: "
            f"expected {subject.llama_alias}, found {sorted(aliases)}"
        )
    return {
        "model_file": model_file,
        "llama_alias": subject.llama_alias,
        "build_info": str(props.get("build_info", "")).strip(),
        "configured_context_tokens": int(
            _mapping(
                props.get("default_generation_settings", {}),
                "llama.props.default_generation_settings",
            ).get("n_ctx", 0)
        ),
        "total_slots": int(props.get("total_slots", 0)),
    }


def fixture_messages(suite: str) -> list[dict[str, str]]:
    if suite == "exact":
        return [{"role": "user", "content": "Reply with exactly ROUTE_OK."}]
    if suite == "planner":
        return [
            {
                "role": "system",
                "content": (
                    "Return only one JSON object with exactly these fields: "
                    "instruction (non-empty string), editable_files (one or two "
                    "repository-relative strings), acceptance_criteria (one to six "
                    "non-empty strings), and depends_on (an array of strings)."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Task: Fix the boundary defect in calculator.py without changing "
                    "other files. Evidence: the function currently uses "
                    "`value < limit`; the requirement and failing test require the "
                    "equal case to pass. Produce one atomic implementation-ready plan."
                ),
            },
        ]
    if suite == "reviewer":
        return [
            {
                "role": "system",
                "content": (
                    "Return only one JSON object with exactly these fields: verdict "
                    "(pass, fail, or needs_attention), summary (non-empty string), "
                    "issues (array of strings), and unrelated_changes "
                    "(array of strings)."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Task: Accept values equal to limit in calculator.py. "
                    "Changed files: [calculator.py]. Verification failed because the "
                    "equal-boundary test still fails. Diff: `return value < limit` was "
                    "left unchanged. Review the result without editing files."
                ),
            },
        ]
    raise DiagnosticError(f"Unsupported suite: {suite}")


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


def _planner_schema_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
    }:
        return False
    return (
        isinstance(value["instruction"], str)
        and bool(value["instruction"].strip())
        and _valid_string_list(value["editable_files"], minimum=1, maximum=2)
        and _valid_string_list(value["acceptance_criteria"], minimum=1, maximum=6)
        and _valid_string_list(value["depends_on"])
    )


def _reviewer_schema_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "verdict",
        "summary",
        "issues",
        "unrelated_changes",
    }:
        return False
    return (
        value["verdict"] in {"pass", "fail", "needs_attention"}
        and isinstance(value["summary"], str)
        and bool(value["summary"].strip())
        and _valid_string_list(value["issues"])
        and _valid_string_list(value["unrelated_changes"])
    )


def _planner_semantics_valid(value: Mapping[str, Any]) -> bool:
    criteria = " ".join(value["acceptance_criteria"]).lower()
    return (
        value["editable_files"] == ["calculator.py"]
        and value["depends_on"] == []
        and any(term in criteria for term in ("equal", "boundary", "<=", "limit"))
    )


def _reviewer_semantics_valid(value: Mapping[str, Any]) -> bool:
    return (
        value["verdict"] == "fail"
        and bool(value["issues"])
        and value["unrelated_changes"] == []
    )


def assess_contract(suite: str, content: str) -> dict[str, Any]:
    """Classify format, schema, and fixture semantics without retaining content."""
    if suite == "exact":
        valid = content.strip() == "ROUTE_OK"
        return {
            "json_valid": None,
            "schema_valid": valid,
            "task_semantics_valid": valid,
            "contract_failures": [] if valid else ["exact_mismatch"],
        }

    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {
            "json_valid": False,
            "schema_valid": False,
            "task_semantics_valid": False,
            "contract_failures": ["non_json_final"],
        }
    if suite == "planner":
        schema = _planner_schema_valid(value)
        semantics = schema and _planner_semantics_valid(value)
    elif suite == "reviewer":
        schema = _reviewer_schema_valid(value)
        semantics = schema and _reviewer_semantics_valid(value)
    else:
        raise DiagnosticError(f"Unsupported suite: {suite}")
    failures: list[str] = []
    if not schema:
        failures.append("schema_mismatch")
    elif not semantics:
        failures.append("task_semantics_mismatch")
    return {
        "json_valid": True,
        "schema_valid": bool(schema),
        "task_semantics_valid": bool(semantics),
        "contract_failures": failures,
    }


def _profile_payload(subject: Subject, suite: str) -> tuple[dict[str, Any], float]:
    if suite == "exact":
        route = subject.routes["exact"]
        payload: dict[str, Any] = {
            "model": route,
            "messages": fixture_messages(suite),
            "temperature": 0.0,
            "max_tokens": 64,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False},
                "thinking_budget_tokens": 0,
            },
        }
        return payload, 135.0

    profile = subject.profiles[suite]
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {"enable_thinking": profile.reasoning_mode == "on"},
        "thinking_budget_tokens": profile.reasoning_tokens,
    }
    if profile.top_k is not None:
        extra_body["top_k"] = profile.top_k
    if profile.repetition_penalty is not None:
        extra_body["repeat_penalty"] = profile.repetition_penalty
    payload = {
        "model": subject.routes[suite],
        "messages": fixture_messages(suite),
        "temperature": profile.temperature,
        "max_tokens": profile.max_tokens,
        "extra_body": extra_body,
    }
    if profile.top_p != 1.0:
        payload["top_p"] = profile.top_p
    return payload, float(profile.timeout_seconds + 15)


def parse_generation_throughput(metrics: str) -> float | None:
    values: list[float] = []
    for line in metrics.splitlines():
        stripped = line.strip()
        if not stripped.startswith("llamacpp:predicted_tokens_seconds"):
            continue
        try:
            value = float(stripped.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return values[-1] if values else None


def run_attempt(
    *,
    client: HttpClient,
    subject: Subject,
    suite: str,
    index: int,
    litellm_base_url: str,
    llama_base_url: str,
) -> dict[str, Any]:
    payload, timeout = _profile_payload(subject, suite)
    started = time.perf_counter()
    try:
        response = client.post_json(
            _url(litellm_base_url, "/chat/completions"),
            payload,
            timeout=timeout,
        )
    except DiagnosticError as exc:
        latency = time.perf_counter() - started
        normalized = normalize_provider_error(
            exc,
            model=subject.routes[suite],
            provider="litellm",
        )
        return {
            "attempt_id": f"{suite}-{index}",
            "suite": suite,
            "route": subject.routes[suite],
            "response_outcome": normalized.outcome,
            "final_answer_present": False,
            "json_valid": None if suite == "exact" else False,
            "schema_valid": False,
            "task_semantics_valid": False,
            "contract_failures": ["response_failure"],
            "reasoning_present": False,
            "malformed_tool_call": False,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": None,
            "latency_seconds": latency,
            "generated_tokens_per_second": 0.0,
        }

    latency = time.perf_counter() - started
    normalized = normalize_model_response(
        response,
        expected_content="ROUTE_OK" if suite == "exact" else None,
        accept_tool_calls=False,
    )
    assessment = assess_contract(suite, normalized.content)
    failures = list(assessment["contract_failures"])
    if normalized.outcome != ROUTE_OK:
        failures.insert(0, "response_failure")
    if not normalized.content.strip() and "empty_final" not in failures:
        failures.append("empty_final")
    if normalized.tool_calls:
        failures.append("malformed_tool_call")
    if normalized.prompt_tokens is None or normalized.completion_tokens is None:
        raise DiagnosticError(f"{suite}-{index} omitted token usage")
    throughput = None
    try:
        throughput = parse_generation_throughput(
            client.get_text(_url(llama_base_url, "/metrics"))
        )
    except DiagnosticError:
        throughput = None
    if throughput is None:
        throughput = normalized.completion_tokens / latency if latency > 0 else 0.0
    unique_failures = list(dict.fromkeys(failures))
    if any(code not in FAILURE_CODES for code in unique_failures):
        raise DiagnosticError("Unsupported contract failure classification")
    return {
        "attempt_id": f"{suite}-{index}",
        "suite": suite,
        "route": subject.routes[suite],
        "response_outcome": normalized.outcome,
        "final_answer_present": bool(normalized.content.strip()),
        **assessment,
        "contract_failures": unique_failures,
        "reasoning_present": normalized.reasoning_present,
        "malformed_tool_call": bool(normalized.tool_calls),
        "prompt_tokens": normalized.prompt_tokens,
        "completion_tokens": normalized.completion_tokens,
        "reasoning_tokens": normalized.reasoning_tokens,
        "latency_seconds": latency,
        "generated_tokens_per_second": throughput,
    }


def _rate(records: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(bool(record[field]) for record in records) / len(records)


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for suite in SUITES:
        selected = [record for record in records if record["suite"] == suite]
        if not selected:
            raise DiagnosticError(f"No records for suite: {suite}")
        json_values = [
            bool(record["json_valid"])
            for record in selected
            if record["json_valid"] is not None
        ]
        failures: dict[str, int] = {}
        for record in selected:
            for code in record["contract_failures"]:
                failures[code] = failures.get(code, 0) + 1
        result[suite] = {
            "attempts": len(selected),
            "final_answer_rate": _rate(selected, "final_answer_present"),
            "json_rate": (sum(json_values) / len(json_values) if json_values else None),
            "schema_rate": _rate(selected, "schema_valid"),
            "task_semantics_rate": _rate(selected, "task_semantics_valid"),
            "reasoning_presence_rate": _rate(selected, "reasoning_present"),
            "failure_counts": dict(sorted(failures.items())),
            "mean_completion_tokens": sum(
                int(record["completion_tokens"]) for record in selected
            )
            / len(selected),
            "mean_latency_seconds": sum(
                float(record["latency_seconds"]) for record in selected
            )
            / len(selected),
            "minimum_generation_tokens_per_second": min(
                float(record["generated_tokens_per_second"]) for record in selected
            ),
        }
    return result


def collect_report(
    *,
    protocol: Protocol,
    subject_name: str,
    environment_id: str,
    implementation_commit: str,
    client: HttpClient,
    litellm_base_url: str,
    llama_base_url: str,
) -> dict[str, Any]:
    if subject_name not in SUBJECTS:
        raise DiagnosticError(f"Unsupported subject: {subject_name}")
    environment_id = _string(environment_id, "environment_id")
    subject = protocol.subjects[subject_name]
    identity = inspect_service(
        client=client,
        llama_base_url=llama_base_url,
        subject=subject,
    )
    records = [
        run_attempt(
            client=client,
            subject=subject,
            suite=suite,
            index=index,
            litellm_base_url=litellm_base_url,
            llama_base_url=llama_base_url,
        )
        for suite in SUITES
        for index in range(1, protocol.attempts_per_suite + 1)
    ]
    final_identity = inspect_service(
        client=client,
        llama_base_url=llama_base_url,
        subject=subject,
    )
    if final_identity != identity:
        raise DiagnosticError("llama.cpp service identity changed during collection")
    report = {
        "schema_version": 1,
        "collection_kind": "route-contract-diagnostic-v2",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "fixture_version": protocol.fixture_version,
        "subject": subject_name,
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "routes": dict(subject.routes),
        "profiles": {
            role: profile.as_dict() for role, profile in subject.profiles.items()
        },
        "implementation_commit": implementation_commit,
        "environment_id": environment_id,
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "service_identity": identity,
        "attempts": records,
        "summary": summarize(records),
        "storage_policy": "no prompt, final-answer, or reasoning text retained",
    }
    report["collection_sha256"] = _sha256(report)
    return report


def _validate_attempt(
    value: Any,
    *,
    subject: Subject,
    suite: str,
    index: int,
) -> Mapping[str, Any]:
    name = f"report.attempts[{suite}-{index}]"
    attempt = _mapping(value, name)
    _exact_keys(
        attempt,
        {
            "attempt_id",
            "suite",
            "route",
            "response_outcome",
            "final_answer_present",
            "json_valid",
            "schema_valid",
            "task_semantics_valid",
            "contract_failures",
            "reasoning_present",
            "malformed_tool_call",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "latency_seconds",
            "generated_tokens_per_second",
        },
        name,
    )
    expected_id = f"{suite}-{index}"
    if attempt["attempt_id"] != expected_id:
        raise DiagnosticError(f"{name}.attempt_id must be {expected_id}")
    if attempt["suite"] != suite:
        raise DiagnosticError(f"{name}.suite must be {suite}")
    if attempt["route"] != subject.routes[suite]:
        raise DiagnosticError(f"{name}.route does not match the protocol")
    outcome = _string(attempt["response_outcome"], f"{name}.response_outcome")
    if outcome not in RESPONSE_OUTCOMES:
        raise DiagnosticError(f"{name}.response_outcome is unsupported")
    final_present = _boolean(
        attempt["final_answer_present"], f"{name}.final_answer_present"
    )
    json_valid = attempt["json_valid"]
    if suite == "exact":
        if json_valid is not None:
            raise DiagnosticError(f"{name}.json_valid must be null for exact attempts")
    else:
        json_valid = _boolean(json_valid, f"{name}.json_valid")
    schema_valid = _boolean(attempt["schema_valid"], f"{name}.schema_valid")
    semantics_valid = _boolean(
        attempt["task_semantics_valid"], f"{name}.task_semantics_valid"
    )
    failures_value = attempt["contract_failures"]
    if not isinstance(failures_value, list):
        raise DiagnosticError(f"{name}.contract_failures must be an array")
    failures = [_string(item, f"{name}.contract_failures") for item in failures_value]
    if len(failures) != len(set(failures)):
        raise DiagnosticError(f"{name}.contract_failures contains duplicates")
    unsupported = sorted(set(failures) - FAILURE_CODES)
    if unsupported:
        raise DiagnosticError(
            f"{name}.contract_failures contains unsupported values: "
            + ", ".join(unsupported)
        )
    reasoning_present = _boolean(
        attempt["reasoning_present"], f"{name}.reasoning_present"
    )
    malformed_tool_call = _boolean(
        attempt["malformed_tool_call"], f"{name}.malformed_tool_call"
    )
    prompt_tokens = _integer(attempt["prompt_tokens"], f"{name}.prompt_tokens")
    completion_tokens = _integer(
        attempt["completion_tokens"], f"{name}.completion_tokens"
    )
    reasoning_tokens = attempt["reasoning_tokens"]
    if reasoning_tokens is not None:
        reasoning_tokens = _integer(reasoning_tokens, f"{name}.reasoning_tokens")
    latency = _number(attempt["latency_seconds"], f"{name}.latency_seconds")
    throughput = _number(
        attempt["generated_tokens_per_second"],
        f"{name}.generated_tokens_per_second",
    )

    if (outcome != ROUTE_OK) != ("response_failure" in failures):
        raise DiagnosticError(
            f"{name}.response_failure classification disagrees with the outcome"
        )
    if malformed_tool_call != ("malformed_tool_call" in failures):
        raise DiagnosticError(
            f"{name}.malformed_tool_call classification is inconsistent"
        )
    if semantics_valid and not schema_valid:
        raise DiagnosticError(f"{name}.task_semantics_valid requires schema_valid")
    if suite == "exact":
        if schema_valid != semantics_valid:
            raise DiagnosticError(
                f"{name} exact schema and semantic validity must agree"
            )
        if schema_valid and failures:
            raise DiagnosticError(f"{name} valid exact response cannot have failures")
        if (
            not schema_valid
            and outcome != PROVIDER_ERROR
            and "exact_mismatch" not in failures
        ):
            raise DiagnosticError(
                f"{name} invalid exact response requires exact_mismatch"
            )
    else:
        assert isinstance(json_valid, bool)
        if schema_valid and not json_valid:
            raise DiagnosticError(f"{name}.schema_valid requires json_valid")
        if (
            not json_valid
            and outcome != PROVIDER_ERROR
            and "non_json_final" not in failures
        ):
            raise DiagnosticError(f"{name} invalid JSON requires non_json_final")
        if json_valid and not schema_valid and "schema_mismatch" not in failures:
            raise DiagnosticError(f"{name} invalid schema requires schema_mismatch")
        if (
            schema_valid
            and not semantics_valid
            and "task_semantics_mismatch" not in failures
        ):
            raise DiagnosticError(
                f"{name} invalid task semantics requires task_semantics_mismatch"
            )
        if semantics_valid and any(
            code in failures
            for code in (
                "non_json_final",
                "schema_mismatch",
                "task_semantics_mismatch",
            )
        ):
            raise DiagnosticError(
                f"{name} valid task semantics conflicts with contract failures"
            )
    if final_present and "empty_final" in failures:
        raise DiagnosticError(f"{name}.empty_final classification is inconsistent")

    return {
        "attempt_id": expected_id,
        "suite": suite,
        "route": subject.routes[suite],
        "response_outcome": outcome,
        "final_answer_present": final_present,
        "json_valid": json_valid,
        "schema_valid": schema_valid,
        "task_semantics_valid": semantics_valid,
        "contract_failures": failures,
        "reasoning_present": reasoning_present,
        "malformed_tool_call": malformed_tool_call,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "latency_seconds": latency,
        "generated_tokens_per_second": throughput,
    }


def validate_report(value: Any, protocol: Protocol) -> Mapping[str, Any]:
    report = _mapping(value, "report")
    required = {
        "schema_version",
        "collection_kind",
        "protocol_id",
        "protocol_sha256",
        "fixture_version",
        "subject",
        "model_file",
        "llama_alias",
        "routes",
        "profiles",
        "implementation_commit",
        "environment_id",
        "collected_at_utc",
        "service_identity",
        "attempts",
        "summary",
        "storage_policy",
        "collection_sha256",
    }
    _exact_keys(report, required, "report")
    if report["schema_version"] != 1:
        raise DiagnosticError("report.schema_version must be 1")
    if report["collection_kind"] != "route-contract-diagnostic-v2":
        raise DiagnosticError("report.collection_kind is unsupported")
    if report["protocol_id"] != protocol.protocol_id:
        raise DiagnosticError("report.protocol_id does not match the protocol")
    if report["protocol_sha256"] != protocol.sha256:
        raise DiagnosticError("report.protocol_sha256 does not match the protocol")
    if report["fixture_version"] != protocol.fixture_version:
        raise DiagnosticError("report.fixture_version does not match the protocol")
    expected_hash = report["collection_sha256"]
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise DiagnosticError("report.collection_sha256 is invalid")
    unhashed = dict(report)
    del unhashed["collection_sha256"]
    if expected_hash != _sha256(unhashed):
        raise DiagnosticError("report.collection_sha256 is invalid")
    subject_name = _string(report["subject"], "report.subject")
    if subject_name not in SUBJECTS:
        raise DiagnosticError("report.subject is unsupported")
    subject = protocol.subjects[subject_name]
    if report["model_file"] != subject.model_file:
        raise DiagnosticError("report.model_file does not match the protocol")
    if report["llama_alias"] != subject.llama_alias:
        raise DiagnosticError("report.llama_alias does not match the protocol")
    if dict(_mapping(report["routes"], "report.routes")) != dict(subject.routes):
        raise DiagnosticError("report.routes do not match the protocol")
    expected_profiles = {
        role: profile.as_dict() for role, profile in subject.profiles.items()
    }
    if dict(_mapping(report["profiles"], "report.profiles")) != expected_profiles:
        raise DiagnosticError("report.profiles do not match the protocol")
    commit = _string(report["implementation_commit"], "report.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise DiagnosticError("report.implementation_commit must be a full Git hash")
    _string(report["environment_id"], "report.environment_id")
    timestamp = _string(report["collected_at_utc"], "report.collected_at_utc")
    try:
        collected_at = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise DiagnosticError("report.collected_at_utc is invalid") from exc
    if collected_at.tzinfo is None:
        raise DiagnosticError("report.collected_at_utc must include a timezone")
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
        raise DiagnosticError("report.service_identity.model_file is inconsistent")
    if identity["llama_alias"] != subject.llama_alias:
        raise DiagnosticError("report.service_identity.llama_alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise DiagnosticError("report.service_identity.build_info must be a string")
    _integer(
        identity["configured_context_tokens"],
        "report.service_identity.configured_context_tokens",
    )
    _integer(identity["total_slots"], "report.service_identity.total_slots", minimum=1)
    attempts = report["attempts"]
    if not isinstance(attempts, list):
        raise DiagnosticError("report.attempts must be an array")
    expected_attempts = protocol.attempts_per_suite * len(SUITES)
    if len(attempts) != expected_attempts:
        raise DiagnosticError(
            f"report requires {expected_attempts} attempts, found {len(attempts)}"
        )
    validated_attempts = [
        _validate_attempt(
            attempts[(suite_index * protocol.attempts_per_suite) + index - 1],
            subject=subject,
            suite=suite,
            index=index,
        )
        for suite_index, suite in enumerate(SUITES)
        for index in range(1, protocol.attempts_per_suite + 1)
    ]
    if report["summary"] != summarize(validated_attempts):
        raise DiagnosticError("report.summary does not match report.attempts")
    if report["storage_policy"] != (
        "no prompt, final-answer, or reasoning text retained"
    ):
        raise DiagnosticError("report.storage_policy is unsupported")
    return report


def compare_reports(
    *,
    protocol: Protocol,
    baseline_value: Any,
    candidate_value: Any,
) -> dict[str, Any]:
    baseline = validate_report(baseline_value, protocol)
    candidate = validate_report(candidate_value, protocol)
    if baseline["subject"] != "baseline":
        raise DiagnosticError("The baseline report is not a baseline subject")
    if candidate["subject"] != "candidate":
        raise DiagnosticError("The candidate report is not a candidate subject")
    if baseline["fixture_version"] != candidate["fixture_version"]:
        raise DiagnosticError("Reports use different fixture versions")
    if baseline["environment_id"] != candidate["environment_id"]:
        raise DiagnosticError("Reports use different environment identifiers")
    if baseline["implementation_commit"] != candidate["implementation_commit"]:
        raise DiagnosticError("Reports use different implementation commits")
    comparison: dict[str, Any] = {}
    for suite in SUITES:
        baseline_metrics = baseline["summary"][suite]
        candidate_metrics = candidate["summary"][suite]
        deltas: dict[str, float | None] = {}
        for field in (
            "final_answer_rate",
            "json_rate",
            "schema_rate",
            "task_semantics_rate",
            "reasoning_presence_rate",
        ):
            baseline_value_field = baseline_metrics[field]
            candidate_value_field = candidate_metrics[field]
            deltas[field] = (
                None
                if baseline_value_field is None or candidate_value_field is None
                else float(candidate_value_field) - float(baseline_value_field)
            )
        comparison[suite] = {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "candidate_minus_baseline": deltas,
        }
    return {
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "fixture_version": protocol.fixture_version,
        "environment_id": baseline["environment_id"],
        "baseline_collection_sha256": baseline["collection_sha256"],
        "candidate_collection_sha256": candidate["collection_sha256"],
        "comparison": comparison,
        "qualification_claim": None,
    }


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    """Atomically create one report without replacing prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DiagnosticError(f"Refusing to overwrite existing report: {path}")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.link(temporary, path)
    except FileExistsError as exc:
        raise DiagnosticError(f"Refusing to overwrite existing report: {path}") from exc
    except OSError as exc:
        raise DiagnosticError(f"Unable to write report {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def default_output_path(subject: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{subject}-f3-contract-v2-{timestamp}.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    subparsers = result.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect one subject report.")
    collect.add_argument("--subject", choices=SUBJECTS, required=True)
    collect.add_argument("--environment-id", required=True)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--litellm-base-url", default=DEFAULT_LITELLM_BASE_URL)
    collect.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)

    compare = subparsers.add_parser("compare", help="Compare baseline and candidate.")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    protocol = load_protocol(arguments.protocol)
    if arguments.command == "collect":
        output = arguments.output or default_output_path(arguments.subject)
        report = collect_report(
            protocol=protocol,
            subject_name=arguments.subject,
            environment_id=arguments.environment_id,
            implementation_commit=git_commit(),
            client=HttpClient(),
            litellm_base_url=arguments.litellm_base_url,
            llama_base_url=arguments.llama_base_url,
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
        baseline_value=load_json(arguments.baseline, "baseline report"),
        candidate_value=load_json(arguments.candidate, "candidate report"),
    )
    if arguments.output:
        write_report(arguments.output, comparison)
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DiagnosticError, subprocess.CalledProcessError) as exc:
        print(f"route-contract-diagnostic: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
