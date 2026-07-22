"""Deterministic normalization of local-coder run evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable

OUTCOME_SCHEMA_VERSION = 1
_DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


class FailureClass(StrEnum):
    """Stable failure taxonomy used by mining and scorecards."""

    VERIFICATION = "verification"
    ORACLE = "oracle"
    REVIEW = "review"
    POLICY = "policy"
    TOOL = "tool"
    EDITOR = "editor"
    SCOPE = "scope"
    BUDGET = "budget"
    ENVIRONMENT = "environment"
    MANIFEST = "manifest"
    PROCESS = "process"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedOutcome:
    """Comparable facts derived from one recorded agent run."""

    schema_version: int
    run_id: str
    status: str
    repository: str
    baseline_commit: str | None
    task_hash: str
    suite_hash: str | None
    diff_hash: str | None
    model_hash: str | None
    route_hash: str | None
    skill_hash: str | None
    configuration_hash: str | None
    expected_changed_paths: tuple[str, ...] | None
    actual_changed_paths: tuple[str, ...]
    verification_result: str
    oracle_result: str
    review_result: str
    policy_result: str
    failures: tuple[str, ...]
    wall_time_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    model_calls: int | None
    tool_calls: int
    verification_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)


def stable_hash(value: Any) -> str:
    """Hash structured data using canonical JSON encoding."""
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_text(value: str) -> str:
    """Hash text without treating it as executable or structured input."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _duration_ms(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds() * 1000
    except ValueError:
        return None


def _last_artifact(details: dict[str, Any], kind: str) -> dict[str, Any] | None:
    artifacts = [row for row in details.get("artifacts", []) if row.get("kind") == kind]
    return artifacts[-1] if artifacts else None


def _review_result(details: dict[str, Any]) -> str:
    artifact = _last_artifact(details, "review")
    if artifact is None or not isinstance(artifact.get("content"), str):
        return "unknown"
    try:
        verdict = json.loads(artifact["content"]).get("verdict")
    except (json.JSONDecodeError, AttributeError):
        return "fail"
    return verdict if verdict in {"pass", "fail", "needs_attention"} else "fail"


def _verification_result(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "unknown"
    return "pass" if bool(rows[-1].get("passed")) else "fail"


def _sum_known(rows: Iterable[dict[str, Any]], field: str) -> int | None:
    values = [row.get(field) for row in rows]
    known = [value for value in values if isinstance(value, int)]
    return sum(known) if known else None


def _diff_facts(details: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    artifact = _last_artifact(details, "diff")
    if artifact is None or not isinstance(artifact.get("content"), str):
        return None, ()
    content = artifact["content"]
    paths: set[str] = set()
    for before, after in _DIFF_PATH.findall(content):
        paths.add(after if after != "/dev/null" else before)
    return hash_text(content), tuple(sorted(paths))


def _identity_hash(rows: list[dict[str, Any]], field: str) -> str | None:
    values = sorted({row[field] for row in rows if isinstance(row.get(field), str)})
    return stable_hash(values) if values else None


def normalize_run(details: dict[str, Any]) -> NormalizedOutcome:
    """Normalize a complete StateStore run record without trusting its prose."""
    verification = list(details.get("verification", []))
    tool_calls = list(details.get("tool_calls", []))
    model_metrics = list(details.get("model_metrics", []))
    agents = list(details.get("agents", []))
    diff_hash, changed_paths = _diff_facts(details)
    verification_result = _verification_result(verification)
    review_result = _review_result(details)
    result_text = (
        details.get("result") if isinstance(details.get("result"), str) else ""
    )
    status = str(details.get("status", "unknown"))
    context = details.get("context")
    context = context if isinstance(context, dict) else {}

    failures: set[str] = set()
    if verification_result == "fail":
        failures.add(FailureClass.VERIFICATION)
    if review_result in {"fail", "needs_attention"}:
        failures.add(FailureClass.REVIEW)
    failed_tools = [row for row in tool_calls if row.get("status") == "error"]
    if failed_tools:
        failures.add(FailureClass.TOOL)
    if any(row.get("tool_name") == "apply_atomic_edit" for row in failed_tools):
        failures.add(FailureClass.EDITOR)
    if any(row.get("tool_name") == "review_diff" for row in failed_tools):
        failures.add(FailureClass.REVIEW)
    if "Scope violations:" in result_text:
        failures.add(FailureClass.SCOPE)
    if "budget" in result_text.lower():
        failures.add(FailureClass.BUDGET)
    if status == "failed":
        failures.add(FailureClass.PROCESS)
    if status == "needs_attention" and not failures:
        failures.add(FailureClass.UNKNOWN)

    if {FailureClass.SCOPE, FailureClass.EDITOR} & failures:
        policy_result = "fail"
    elif status in {"awaiting_approval", "no_changes"}:
        policy_result = "pass"
    else:
        policy_result = "unknown"
    task = details.get("task")
    if not isinstance(task, str):
        task = ""
    expected_changed_paths = None
    encoded_paths = context.get("expected_changed_paths")
    if isinstance(encoded_paths, str):
        try:
            decoded_paths = json.loads(encoded_paths)
        except json.JSONDecodeError:
            decoded_paths = None
        if isinstance(decoded_paths, list) and all(
            isinstance(path, str) for path in decoded_paths
        ):
            expected_changed_paths = tuple(sorted(set(decoded_paths)))

    return NormalizedOutcome(
        schema_version=OUTCOME_SCHEMA_VERSION,
        run_id=str(details.get("id", "unknown")),
        status=status,
        repository=str(details.get("repository", "unknown")),
        baseline_commit=context.get("baseline_commit"),
        task_hash=hash_text(task),
        suite_hash=context.get("suite_hash"),
        diff_hash=diff_hash,
        model_hash=context.get("model_hash"),
        route_hash=_identity_hash(agents, "model_route"),
        skill_hash=_identity_hash(agents, "skill"),
        configuration_hash=context.get("configuration_hash"),
        expected_changed_paths=expected_changed_paths,
        actual_changed_paths=changed_paths,
        verification_result=verification_result,
        oracle_result="unknown",
        review_result=review_result,
        policy_result=policy_result,
        failures=tuple(sorted(str(item) for item in failures)),
        wall_time_ms=_duration_ms(details.get("created_at"), details.get("updated_at")),
        prompt_tokens=_sum_known(model_metrics, "prompt_tokens"),
        completion_tokens=_sum_known(model_metrics, "completion_tokens"),
        model_calls=len(model_metrics) if model_metrics else None,
        tool_calls=len(tool_calls),
        verification_count=len(verification),
    )


def analyze_run_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return normalized outcomes and deterministic failure clusters."""
    outcomes = [normalize_run(record) for record in records]
    clusters: dict[str, list[str]] = {}
    for outcome in outcomes:
        for failure in outcome.failures:
            clusters.setdefault(failure, []).append(outcome.run_id)
    return {
        "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
        "run_count": len(outcomes),
        "failure_clusters": {
            name: sorted(run_ids) for name, run_ids in sorted(clusters.items())
        },
        "outcomes": [outcome.to_dict() for outcome in outcomes],
    }
