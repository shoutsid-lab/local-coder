"""Strict loading and leakage checks for the Track G real-task corpus."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .outcomes import stable_hash

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEVELOPMENT_PATH = (
    ROOT / "evaluation" / "real_task_cases" / "development-v1.json"
)
DEFAULT_HOLDOUT_INDEX_PATH = (
    ROOT / "evaluation" / "real_task_cases" / "holdout-v1.index.json"
)
DEFAULT_HOLDOUT_SUITE_PATH = (
    ROOT / ".local-coder" / "real-task-holdout" / "holdout-v1.json"
)

SCHEMA_VERSION = 1
CORPUS_ID = "local-coder-real-tasks-v1"
DEVELOPMENT_CASES = 8
HOLDOUT_CASES = 4
ROLES = frozenset({"planner", "reviewer"})
VISIBILITIES = frozenset({"development", "holdout"})
CASE_CLASSES = frozenset(
    {
        "exact_one_file_repair",
        "test_or_lint_failure_repair",
        "multi_file_bounded_change",
        "planning_or_evidence_selection_failure",
        "reviewer_defect_detection",
        "documentation_or_interface_consistency",
    }
)
CLASS_MINIMUMS = {
    "exact_one_file_repair": 3,
    "test_or_lint_failure_repair": 2,
    "multi_file_bounded_change": 2,
    "planning_or_evidence_selection_failure": 2,
    "reviewer_defect_detection": 2,
    "documentation_or_interface_consistency": 1,
}
ALLOWED_TAGS = frozenset(
    {
        "adapter_boundary",
        "ambiguous_scope",
        "documentation",
        "false_positive_trap",
        "lint",
        "multi_file",
        "reasoning_contract",
        "review_state",
        "route_policy",
        "scope_control",
        "structured_output",
        "verification_evidence",
    }
)
DIFFICULTIES = frozenset({"medium", "high"})
BASELINE_KINDS = frozenset({"git_commit", "archived_run", "evidence_snapshot"})
REVIEW_VERDICTS = frozenset({"pass", "fail", "needs_attention"})
HEX_RE = re.compile(r"^[0-9a-f]+$")
PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
FORBIDDEN_TEXT = ("/home/", "/mnt/c/", "C:\\Users\\", "BEGIN PRIVATE KEY")


class RealTaskCorpusError(ValueError):
    """Raised when trusted Track G case data is malformed or leaks holdout data."""


@dataclass(frozen=True)
class RealTaskCase:
    """One validated planner or reviewer case with its trusted oracle."""

    case_id: str
    role: str
    case_class: str
    tags: tuple[str, ...]
    difficulty: str
    pattern_group: str
    baseline: Mapping[str, str]
    task: str
    inputs: Mapping[str, Any]
    expected_scope: tuple[str, ...]
    verification_commands: tuple[str, ...]
    provenance: Mapping[str, str]
    successful_outcome: Mapping[str, Any]
    oracle: Mapping[str, Any]
    case_hash: str


@dataclass(frozen=True)
class CaseSuite:
    """A complete development or trusted holdout suite."""

    corpus_id: str
    suite_id: str
    visibility: str
    cases: tuple[RealTaskCase, ...]
    suite_hash: str
    path: Path


@dataclass(frozen=True)
class HoldoutIndexEntry:
    """Candidate-visible metadata for one sealed holdout case."""

    case_id: str
    role: str
    case_class: str
    tags: tuple[str, ...]
    difficulty: str
    pattern_group: str
    baseline_kind: str
    provenance_reference: str
    sealed_case_sha256: str


@dataclass(frozen=True)
class HoldoutIndex:
    """Committed metadata and hashes for a candidate-inaccessible holdout suite."""

    corpus_id: str
    suite_id: str
    cases: tuple[HoldoutIndexEntry, ...]
    sealed_suite_sha256: str
    index_hash: str
    path: Path


@dataclass(frozen=True)
class CorpusSummary:
    """Non-sensitive identity and coverage summary for the frozen corpus."""

    corpus_id: str
    development_suite_id: str
    development_sha256: str
    holdout_suite_id: str
    holdout_index_sha256: str
    sealed_holdout_sha256: str
    total_cases: int
    role_counts: Mapping[str, int]
    class_counts: Mapping[str, int]
    trusted_holdout_loaded: bool

    def model_output(self) -> dict[str, Any]:
        """Return a stable JSON-compatible summary."""
        return {
            "corpus_id": self.corpus_id,
            "development_suite_id": self.development_suite_id,
            "development_sha256": self.development_sha256,
            "holdout_suite_id": self.holdout_suite_id,
            "holdout_index_sha256": self.holdout_index_sha256,
            "sealed_holdout_sha256": self.sealed_holdout_sha256,
            "total_cases": self.total_cases,
            "role_counts": dict(self.role_counts),
            "class_counts": dict(self.class_counts),
            "trusted_holdout_loaded": self.trusted_holdout_loaded,
        }


def _read_object(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RealTaskCorpusError(f"Unable to read {name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RealTaskCorpusError(f"Invalid JSON in {name}: {path}") from exc
    if not isinstance(value, dict):
        raise RealTaskCorpusError(f"{name} must be a JSON object")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RealTaskCorpusError(f"{name} must be an object")
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
    raise RealTaskCorpusError(f"{name} has invalid fields: {'; '.join(details)}")


def _text(value: Any, name: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise RealTaskCorpusError(f"{name} must contain at least {minimum} characters")
    result = value.strip()
    if any(marker in result for marker in FORBIDDEN_TEXT):
        raise RealTaskCorpusError(f"{name} contains machine-specific or secret text")
    return result


def _identifier(value: Any, name: str) -> str:
    result = _text(value, name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", result):
        raise RealTaskCorpusError(f"{name} must be a lowercase hyphenated identifier")
    return result


def _string_list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    unique: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RealTaskCorpusError(f"{name} must be an array")
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        upper = "unbounded" if maximum is None else str(maximum)
        raise RealTaskCorpusError(
            f"{name} must contain between {minimum} and {upper} items"
        )
    result = tuple(_text(item, f"{name}[{index}]") for index, item in enumerate(value))
    if unique and len(result) != len(set(result)):
        raise RealTaskCorpusError(f"{name} must not contain duplicates")
    return result


def _path(value: Any, name: str) -> str:
    result = _text(value, name)
    if not PATH_RE.fullmatch(result):
        raise RealTaskCorpusError(f"{name} must be a safe repository-relative path")
    return result


def _path_list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> tuple[str, ...]:
    strings = _string_list(value, name, minimum=minimum, maximum=maximum)
    return tuple(_path(item, f"{name}[{index}]") for index, item in enumerate(strings))


def _sha256(value: Any, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or not HEX_RE.fullmatch(result):
        raise RealTaskCorpusError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _baseline(value: Any, name: str, inputs: Mapping[str, Any]) -> Mapping[str, str]:
    data = _mapping(value, name)
    _exact_keys(data, {"repository", "kind", "identity"}, name)
    repository = _text(data["repository"], f"{name}.repository")
    if repository != "shoutsid-lab/local-coder":
        raise RealTaskCorpusError(f"{name}.repository is unsupported")
    kind = _text(data["kind"], f"{name}.kind")
    if kind not in BASELINE_KINDS:
        raise RealTaskCorpusError(f"{name}.kind is unsupported")
    identity = _text(data["identity"], f"{name}.identity")
    expected_length = {
        "git_commit": 40,
        "archived_run": 12,
        "evidence_snapshot": 64,
    }[kind]
    if len(identity) != expected_length or not HEX_RE.fullmatch(identity):
        raise RealTaskCorpusError(
            f"{name}.identity must be {expected_length} lowercase "
            "hexadecimal characters"
        )
    if kind == "evidence_snapshot" and identity != stable_hash(inputs):
        raise RealTaskCorpusError(
            f"{name}.identity does not match the immutable input snapshot"
        )
    return {"repository": repository, "kind": kind, "identity": identity}


def _planner_inputs(value: Any, task: str, name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    _exact_keys(data, {"task", "delegated_task", "repository_evidence"}, name)
    if _text(data["task"], f"{name}.task", minimum=40) != task:
        raise RealTaskCorpusError(f"{name}.task must match the authoritative task")
    delegated = _text(data["delegated_task"], f"{name}.delegated_task", minimum=30)
    evidence = _string_list(
        data["repository_evidence"],
        f"{name}.repository_evidence",
        minimum=4,
        maximum=12,
    )
    return {
        "task": task,
        "delegated_task": delegated,
        "repository_evidence": list(evidence),
    }


def _reviewer_inputs(value: Any, task: str, name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {"task", "changed_files", "verification_passed", "verification_output", "diff"},
        name,
    )
    if _text(data["task"], f"{name}.task", minimum=40) != task:
        raise RealTaskCorpusError(f"{name}.task must match the authoritative task")
    changed_files = _path_list(
        data["changed_files"], f"{name}.changed_files", minimum=1, maximum=4
    )
    if not isinstance(data["verification_passed"], bool):
        raise RealTaskCorpusError(f"{name}.verification_passed must be boolean")
    verification_output = _text(
        data["verification_output"], f"{name}.verification_output", minimum=20
    )
    diff = _text(data["diff"], f"{name}.diff", minimum=160)
    if "diff --git " not in diff or "@@" not in diff:
        raise RealTaskCorpusError(f"{name}.diff must contain a real unified Git diff")
    for changed_file in changed_files:
        if f"a/{changed_file}" not in diff and f"b/{changed_file}" not in diff:
            raise RealTaskCorpusError(
                f"{name}.diff does not contain changed file {changed_file}"
            )
    return {
        "task": task,
        "changed_files": list(changed_files),
        "verification_passed": data["verification_passed"],
        "verification_output": verification_output,
        "diff": diff,
    }


def _planner_oracle(value: Any, name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {
            "editable_files",
            "depends_on",
            "required_instruction_terms",
            "required_acceptance_terms",
            "forbidden_files",
        },
        name,
    )
    return {
        "editable_files": list(
            _path_list(
                data["editable_files"],
                f"{name}.editable_files",
                minimum=1,
                maximum=2,
            )
        ),
        "depends_on": list(
            _string_list(data["depends_on"], f"{name}.depends_on", maximum=6)
        ),
        "required_instruction_terms": list(
            _string_list(
                data["required_instruction_terms"],
                f"{name}.required_instruction_terms",
                minimum=2,
                maximum=8,
            )
        ),
        "required_acceptance_terms": list(
            _string_list(
                data["required_acceptance_terms"],
                f"{name}.required_acceptance_terms",
                minimum=2,
                maximum=8,
            )
        ),
        "forbidden_files": list(
            _path_list(data["forbidden_files"], f"{name}.forbidden_files", maximum=8)
        ),
    }


def _reviewer_oracle(value: Any, name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {
            "verdict",
            "required_issue_paths",
            "required_unrelated_paths",
            "forbidden_issue_paths",
        },
        name,
    )
    verdict = _text(data["verdict"], f"{name}.verdict")
    if verdict not in REVIEW_VERDICTS:
        raise RealTaskCorpusError(f"{name}.verdict is unsupported")
    required_issues = _path_list(
        data["required_issue_paths"], f"{name}.required_issue_paths", maximum=6
    )
    required_unrelated = _path_list(
        data["required_unrelated_paths"],
        f"{name}.required_unrelated_paths",
        maximum=6,
    )
    forbidden_issues = _path_list(
        data["forbidden_issue_paths"], f"{name}.forbidden_issue_paths", maximum=6
    )
    if verdict == "pass" and (required_issues or required_unrelated):
        raise RealTaskCorpusError(f"{name} pass verdict cannot require issues")
    if verdict == "fail" and not (required_issues or required_unrelated):
        raise RealTaskCorpusError(f"{name} fail verdict must identify a defect path")
    return {
        "verdict": verdict,
        "required_issue_paths": list(required_issues),
        "required_unrelated_paths": list(required_unrelated),
        "forbidden_issue_paths": list(forbidden_issues),
    }


def _provenance(value: Any, name: str) -> Mapping[str, str]:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {"source_type", "reference", "observed_failure", "historical_fix"},
        name,
    )
    return {
        "source_type": _text(data["source_type"], f"{name}.source_type"),
        "reference": _text(data["reference"], f"{name}.reference"),
        "observed_failure": _text(
            data["observed_failure"], f"{name}.observed_failure", minimum=20
        ),
        "historical_fix": _text(
            data["historical_fix"], f"{name}.historical_fix", minimum=20
        ),
    }


def _successful_outcome(value: Any, name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    _exact_keys(data, {"changed_files", "summary", "verification_passed"}, name)
    changed_files = _path_list(
        data["changed_files"], f"{name}.changed_files", minimum=1, maximum=4
    )
    if data["verification_passed"] is not True:
        raise RealTaskCorpusError(f"{name}.verification_passed must be true")
    return {
        "changed_files": list(changed_files),
        "summary": _text(data["summary"], f"{name}.summary", minimum=20),
        "verification_passed": True,
    }


def _case(value: Any, name: str) -> RealTaskCase:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {
            "id",
            "role",
            "case_class",
            "tags",
            "difficulty",
            "pattern_group",
            "baseline",
            "task",
            "inputs",
            "expected_scope",
            "verification_commands",
            "provenance",
            "successful_outcome",
            "oracle",
        },
        name,
    )
    case_id = _identifier(data["id"], f"{name}.id")
    role = _text(data["role"], f"{name}.role")
    if role not in ROLES:
        raise RealTaskCorpusError(f"{name}.role is unsupported")
    case_class = _text(data["case_class"], f"{name}.case_class")
    if case_class not in CASE_CLASSES:
        raise RealTaskCorpusError(f"{name}.case_class is unsupported")
    tags = _string_list(data["tags"], f"{name}.tags", minimum=1, maximum=4)
    if any(tag not in ALLOWED_TAGS for tag in tags):
        raise RealTaskCorpusError(f"{name}.tags contain unsupported values")
    difficulty = _text(data["difficulty"], f"{name}.difficulty")
    if difficulty not in DIFFICULTIES:
        raise RealTaskCorpusError(f"{name}.difficulty is unsupported")
    pattern_group = _identifier(data["pattern_group"], f"{name}.pattern_group")
    task = _text(data["task"], f"{name}.task", minimum=40)
    raw_inputs = _mapping(data["inputs"], f"{name}.inputs")
    inputs = (
        _planner_inputs(raw_inputs, task, f"{name}.inputs")
        if role == "planner"
        else _reviewer_inputs(raw_inputs, task, f"{name}.inputs")
    )
    baseline = _baseline(data["baseline"], f"{name}.baseline", raw_inputs)
    expected_scope = _path_list(
        data["expected_scope"], f"{name}.expected_scope", minimum=1, maximum=4
    )
    verification_commands = _string_list(
        data["verification_commands"],
        f"{name}.verification_commands",
        minimum=1,
        maximum=4,
    )
    for command in verification_commands:
        if not command.startswith(("make ", ".venv/bin/python ", "git diff --check")):
            raise RealTaskCorpusError(
                f"{name}.verification_commands contains an unsupported command"
            )
    provenance = _provenance(data["provenance"], f"{name}.provenance")
    successful_outcome = _successful_outcome(
        data["successful_outcome"], f"{name}.successful_outcome"
    )
    oracle = (
        _planner_oracle(data["oracle"], f"{name}.oracle")
        if role == "planner"
        else _reviewer_oracle(data["oracle"], f"{name}.oracle")
    )
    if tuple(successful_outcome["changed_files"]) != expected_scope:
        raise RealTaskCorpusError(
            f"{name}.successful_outcome changed files must equal expected_scope"
        )
    if role == "planner" and tuple(oracle["editable_files"]) != expected_scope:
        raise RealTaskCorpusError(
            f"{name}.planner oracle editable files must equal expected_scope"
        )
    normalized = {
        "id": case_id,
        "role": role,
        "case_class": case_class,
        "tags": list(tags),
        "difficulty": difficulty,
        "pattern_group": pattern_group,
        "baseline": dict(baseline),
        "task": task,
        "inputs": dict(inputs),
        "expected_scope": list(expected_scope),
        "verification_commands": list(verification_commands),
        "provenance": dict(provenance),
        "successful_outcome": dict(successful_outcome),
        "oracle": dict(oracle),
    }
    return RealTaskCase(
        case_id=case_id,
        role=role,
        case_class=case_class,
        tags=tags,
        difficulty=difficulty,
        pattern_group=pattern_group,
        baseline=baseline,
        task=task,
        inputs=inputs,
        expected_scope=expected_scope,
        verification_commands=verification_commands,
        provenance=provenance,
        successful_outcome=successful_outcome,
        oracle=oracle,
        case_hash=stable_hash(dict(data)),
    )


def load_case_suite(path: Path, *, expected_visibility: str) -> CaseSuite:
    """Load a complete development or trusted holdout suite."""
    if expected_visibility not in VISIBILITIES:
        raise RealTaskCorpusError("expected_visibility is unsupported")
    payload = _read_object(path.resolve(), f"{expected_visibility} suite")
    _exact_keys(
        payload,
        {"schema_version", "corpus_id", "suite_id", "visibility", "cases"},
        "suite",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise RealTaskCorpusError("suite.schema_version must be 1")
    if payload["corpus_id"] != CORPUS_ID:
        raise RealTaskCorpusError("suite.corpus_id is unsupported")
    if payload["visibility"] != expected_visibility:
        raise RealTaskCorpusError("suite.visibility does not match")
    suite_id = _identifier(payload["suite_id"], "suite.suite_id")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RealTaskCorpusError("suite.cases must be a non-empty array")
    cases = tuple(
        _case(value, f"suite.cases[{index}]") for index, value in enumerate(raw_cases)
    )
    if len({case.case_id for case in cases}) != len(cases):
        raise RealTaskCorpusError("suite case IDs must be unique")
    if len({case.pattern_group for case in cases}) != len(cases):
        raise RealTaskCorpusError("suite pattern groups must be unique")
    return CaseSuite(
        corpus_id=CORPUS_ID,
        suite_id=suite_id,
        visibility=expected_visibility,
        cases=cases,
        suite_hash=stable_hash(payload),
        path=path.resolve(),
    )


def _holdout_index_entry(value: Any, name: str) -> HoldoutIndexEntry:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {
            "id",
            "role",
            "case_class",
            "tags",
            "difficulty",
            "pattern_group",
            "baseline_kind",
            "provenance_reference",
            "sealed_case_sha256",
        },
        name,
    )
    role = _text(data["role"], f"{name}.role")
    if role not in ROLES:
        raise RealTaskCorpusError(f"{name}.role is unsupported")
    case_class = _text(data["case_class"], f"{name}.case_class")
    if case_class not in CASE_CLASSES:
        raise RealTaskCorpusError(f"{name}.case_class is unsupported")
    tags = _string_list(data["tags"], f"{name}.tags", minimum=1, maximum=4)
    if any(tag not in ALLOWED_TAGS for tag in tags):
        raise RealTaskCorpusError(f"{name}.tags contain unsupported values")
    difficulty = _text(data["difficulty"], f"{name}.difficulty")
    if difficulty not in DIFFICULTIES:
        raise RealTaskCorpusError(f"{name}.difficulty is unsupported")
    baseline_kind = _text(data["baseline_kind"], f"{name}.baseline_kind")
    if baseline_kind not in BASELINE_KINDS:
        raise RealTaskCorpusError(f"{name}.baseline_kind is unsupported")
    return HoldoutIndexEntry(
        case_id=_identifier(data["id"], f"{name}.id"),
        role=role,
        case_class=case_class,
        tags=tags,
        difficulty=difficulty,
        pattern_group=_identifier(data["pattern_group"], f"{name}.pattern_group"),
        baseline_kind=baseline_kind,
        provenance_reference=_text(
            data["provenance_reference"], f"{name}.provenance_reference"
        ),
        sealed_case_sha256=_sha256(
            data["sealed_case_sha256"], f"{name}.sealed_case_sha256"
        ),
    )


def load_holdout_index(path: Path) -> HoldoutIndex:
    """Load committed holdout metadata without exposing tasks or oracles."""
    payload = _read_object(path.resolve(), "holdout index")
    _exact_keys(
        payload,
        {
            "schema_version",
            "corpus_id",
            "suite_id",
            "visibility",
            "sealed_suite_sha256",
            "cases",
        },
        "holdout index",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise RealTaskCorpusError("holdout index schema_version must be 1")
    if payload["corpus_id"] != CORPUS_ID:
        raise RealTaskCorpusError("holdout index corpus_id is unsupported")
    if payload["visibility"] != "holdout-index":
        raise RealTaskCorpusError("holdout index visibility must be holdout-index")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RealTaskCorpusError("holdout index cases must be non-empty")
    cases = tuple(
        _holdout_index_entry(value, f"holdout index.cases[{index}]")
        for index, value in enumerate(raw_cases)
    )
    if len({case.case_id for case in cases}) != len(cases):
        raise RealTaskCorpusError("holdout index case IDs must be unique")
    if len({case.pattern_group for case in cases}) != len(cases):
        raise RealTaskCorpusError("holdout index pattern groups must be unique")
    return HoldoutIndex(
        corpus_id=CORPUS_ID,
        suite_id=_identifier(payload["suite_id"], "holdout index.suite_id"),
        cases=cases,
        sealed_suite_sha256=_sha256(
            payload["sealed_suite_sha256"], "holdout index.sealed_suite_sha256"
        ),
        index_hash=stable_hash(payload),
        path=path.resolve(),
    )


def validate_holdout_suite(index: HoldoutIndex, suite: CaseSuite) -> None:
    """Bind a trusted holdout suite to its committed candidate-visible index."""
    if suite.visibility != "holdout":
        raise RealTaskCorpusError("trusted holdout suite visibility is invalid")
    if suite.corpus_id != index.corpus_id or suite.suite_id != index.suite_id:
        raise RealTaskCorpusError("holdout index and trusted suite identities differ")
    if suite.suite_hash != index.sealed_suite_sha256:
        raise RealTaskCorpusError("trusted holdout suite hash does not match the index")
    indexed = {entry.case_id: entry for entry in index.cases}
    if set(indexed) != {case.case_id for case in suite.cases}:
        raise RealTaskCorpusError("holdout index and trusted suite case IDs differ")
    for case in suite.cases:
        entry = indexed[case.case_id]
        expected_metadata = (
            case.role,
            case.case_class,
            case.tags,
            case.difficulty,
            case.pattern_group,
            case.baseline["kind"],
            case.provenance["reference"],
            case.case_hash,
        )
        actual_metadata = (
            entry.role,
            entry.case_class,
            entry.tags,
            entry.difficulty,
            entry.pattern_group,
            entry.baseline_kind,
            entry.provenance_reference,
            entry.sealed_case_sha256,
        )
        if expected_metadata != actual_metadata:
            raise RealTaskCorpusError(
                f"holdout index metadata does not match sealed case {case.case_id}"
            )


def validate_corpus(
    development: CaseSuite,
    holdout_index: HoldoutIndex,
    *,
    holdout_suite: CaseSuite | None = None,
) -> CorpusSummary:
    """Validate split sizes, coverage, uniqueness, and optional holdout binding."""
    if development.visibility != "development":
        raise RealTaskCorpusError("development suite visibility is invalid")
    if development.corpus_id != holdout_index.corpus_id:
        raise RealTaskCorpusError("development and holdout corpus IDs differ")
    if len(development.cases) != DEVELOPMENT_CASES:
        raise RealTaskCorpusError(
            f"development suite must contain exactly {DEVELOPMENT_CASES} cases"
        )
    if len(holdout_index.cases) != HOLDOUT_CASES:
        raise RealTaskCorpusError(
            f"holdout index must contain exactly {HOLDOUT_CASES} cases"
        )
    dev_ids = {case.case_id for case in development.cases}
    holdout_ids = {case.case_id for case in holdout_index.cases}
    if dev_ids & holdout_ids:
        raise RealTaskCorpusError("development and holdout case IDs overlap")
    dev_patterns = {case.pattern_group for case in development.cases}
    holdout_patterns = {case.pattern_group for case in holdout_index.cases}
    if dev_patterns & holdout_patterns:
        raise RealTaskCorpusError("development and holdout pattern groups overlap")

    all_metadata = [(case.role, case.case_class) for case in development.cases] + [
        (case.role, case.case_class) for case in holdout_index.cases
    ]
    role_counts = Counter(role for role, _ in all_metadata)
    class_counts = Counter(case_class for _, case_class in all_metadata)
    if any(role_counts[role] < 5 for role in ROLES):
        raise RealTaskCorpusError("corpus must contain at least five cases per role")
    for case_class, minimum in CLASS_MINIMUMS.items():
        if class_counts[case_class] < minimum:
            raise RealTaskCorpusError(
                f"corpus requires at least {minimum} {case_class} cases"
            )
    if holdout_suite is not None:
        validate_holdout_suite(holdout_index, holdout_suite)

    return CorpusSummary(
        corpus_id=development.corpus_id,
        development_suite_id=development.suite_id,
        development_sha256=development.suite_hash,
        holdout_suite_id=holdout_index.suite_id,
        holdout_index_sha256=holdout_index.index_hash,
        sealed_holdout_sha256=holdout_index.sealed_suite_sha256,
        total_cases=len(all_metadata),
        role_counts=dict(sorted(role_counts.items())),
        class_counts=dict(sorted(class_counts.items())),
        trusted_holdout_loaded=holdout_suite is not None,
    )


def load_default_corpus(
    *,
    holdout_suite_path: Path | None = None,
) -> CorpusSummary:
    """Load the committed corpus and optionally bind the trusted holdout payload."""
    development = load_case_suite(
        DEFAULT_DEVELOPMENT_PATH, expected_visibility="development"
    )
    holdout_index = load_holdout_index(DEFAULT_HOLDOUT_INDEX_PATH)
    holdout_suite = (
        None
        if holdout_suite_path is None
        else load_case_suite(holdout_suite_path, expected_visibility="holdout")
    )
    return validate_corpus(
        development,
        holdout_index,
        holdout_suite=holdout_suite,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--development", type=Path, default=DEFAULT_DEVELOPMENT_PATH)
    result.add_argument(
        "--holdout-index", type=Path, default=DEFAULT_HOLDOUT_INDEX_PATH
    )
    result.add_argument("--holdout-suite", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    development = load_case_suite(
        arguments.development, expected_visibility="development"
    )
    holdout_index = load_holdout_index(arguments.holdout_index)
    holdout_suite = (
        None
        if arguments.holdout_suite is None
        else load_case_suite(arguments.holdout_suite, expected_visibility="holdout")
    )
    summary = validate_corpus(
        development,
        holdout_index,
        holdout_suite=holdout_suite,
    )
    print(json.dumps(summary.model_output(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RealTaskCorpusError as exc:
        print(f"real-task-corpus: {exc}")
        raise SystemExit(1) from exc
