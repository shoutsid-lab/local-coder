"""Strict loading and hashing for trusted evaluation manifests and oracles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .outcomes import stable_hash

MANIFEST_SCHEMA_VERSION = 1
ALLOWED_SCENARIOS = {
    "ambiguous_match",
    "complete_diff_capture",
    "empty_untracked_diff",
    "exact_edit",
    "malformed_editor_output",
    "malformed_reviewer_output",
    "missing_match",
    "multi_edit_atomicity",
    "protected_alias",
    "scope_leakage",
    "sequential_edits",
    "verification_failure",
}


class ManifestError(ValueError):
    """Raised when trusted evaluation input is malformed or mismatched."""


@dataclass(frozen=True)
class EvaluationCase:
    """One allowlisted base-owned evaluation scenario."""

    case_id: str
    scenario: str
    oracle: dict[str, Any] | None


@dataclass(frozen=True)
class SuiteManifest:
    """A validated, immutable development or holdout suite."""

    suite_id: str
    visibility: str
    cases: tuple[EvaluationCase, ...]
    manifest_hash: str
    path: Path


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Could not load trusted JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"Trusted JSON must be an object: {path}")
    return value


def load_suite(path: Path, *, expected_visibility: str) -> SuiteManifest:
    """Load a strict suite and calculate its canonical manifest hash."""
    payload = _read_object(path.resolve())
    if set(payload) != {"schema_version", "suite_id", "visibility", "cases"}:
        raise ManifestError("Suite manifest has unsupported fields.")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("Suite manifest schema version mismatch.")
    if payload["visibility"] != expected_visibility:
        raise ManifestError("Suite manifest visibility mismatch.")
    if not isinstance(payload["suite_id"], str) or not payload["suite_id"]:
        raise ManifestError("Suite ID must be non-empty text.")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise ManifestError("Suite must contain at least one case.")

    cases: list[EvaluationCase] = []
    case_ids: set[str] = set()
    expected_fields = (
        {"id", "scenario", "oracle"}
        if expected_visibility == "development"
        else {"id", "scenario"}
    )
    for raw in payload["cases"]:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ManifestError("Suite case has unsupported fields.")
        case_id = raw["id"]
        scenario = raw["scenario"]
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise ManifestError("Suite case IDs must be unique non-empty text.")
        if scenario not in ALLOWED_SCENARIOS:
            raise ManifestError(f"Scenario is not allowlisted: {scenario}")
        oracle = raw.get("oracle")
        if oracle is not None and not isinstance(oracle, dict):
            raise ManifestError("Development oracle must be an object.")
        cases.append(EvaluationCase(case_id, scenario, oracle))
        case_ids.add(case_id)
    return SuiteManifest(
        suite_id=payload["suite_id"],
        visibility=payload["visibility"],
        cases=tuple(cases),
        manifest_hash=stable_hash(payload),
        path=path.resolve(),
    )


def load_holdout_oracle(
    path: Path,
    manifest: SuiteManifest,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Load a base-owned holdout oracle and require an exact case-ID match."""
    payload = _read_object(path.resolve())
    if set(payload) != {"schema_version", "suite_id", "cases"}:
        raise ManifestError("Holdout oracle has unsupported fields.")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("Holdout oracle schema version mismatch.")
    if payload["suite_id"] != manifest.suite_id:
        raise ManifestError("Holdout manifest and oracle suite IDs differ.")
    cases = payload["cases"]
    if not isinstance(cases, dict) or not all(
        isinstance(case_id, str) and isinstance(value, dict)
        for case_id, value in cases.items()
    ):
        raise ManifestError("Holdout oracle cases must map IDs to objects.")
    expected = {case.case_id for case in manifest.cases}
    if set(cases) != expected:
        raise ManifestError("Holdout manifest and oracle case IDs differ.")
    return cases, stable_hash(payload)
