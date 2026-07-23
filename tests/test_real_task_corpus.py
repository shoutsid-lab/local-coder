from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evaluation.outcomes import stable_hash
from evaluation.real_task_corpus import (
    DEFAULT_DEVELOPMENT_PATH,
    DEFAULT_HOLDOUT_INDEX_PATH,
    RealTaskCorpusError,
    load_case_suite,
    load_holdout_index,
    validate_corpus,
    validate_holdout_suite,
)

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_HOLDOUT = ROOT / ".local-coder" / "real-task-holdout" / "holdout-v1.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_committed_real_task_corpus_is_frozen_and_balanced() -> None:
    development = load_case_suite(
        DEFAULT_DEVELOPMENT_PATH, expected_visibility="development"
    )
    holdout_index = load_holdout_index(DEFAULT_HOLDOUT_INDEX_PATH)

    summary = validate_corpus(development, holdout_index)

    assert summary.total_cases == 12
    assert summary.role_counts == {"planner": 6, "reviewer": 6}
    assert summary.class_counts == {
        "documentation_or_interface_consistency": 1,
        "exact_one_file_repair": 3,
        "multi_file_bounded_change": 2,
        "planning_or_evidence_selection_failure": 2,
        "reviewer_defect_detection": 2,
        "test_or_lint_failure_repair": 2,
    }
    assert summary.trusted_holdout_loaded is False


def test_committed_holdout_index_contains_no_tasks_inputs_or_oracles() -> None:
    payload = _load(DEFAULT_HOLDOUT_INDEX_PATH)
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["visibility"] == "holdout-index"
    assert len(payload["cases"]) == 4
    assert '"task"' not in serialized
    assert '"inputs"' not in serialized
    assert '"oracle"' not in serialized
    assert '"successful_outcome"' not in serialized


def test_development_cases_use_realistic_program_inputs() -> None:
    suite = load_case_suite(DEFAULT_DEVELOPMENT_PATH, expected_visibility="development")

    planner_cases = [case for case in suite.cases if case.role == "planner"]
    reviewer_cases = [case for case in suite.cases if case.role == "reviewer"]

    assert len(planner_cases) == 4
    assert len(reviewer_cases) == 4
    assert all(len(case.inputs["repository_evidence"]) >= 5 for case in planner_cases)
    assert all("diff --git " in case.inputs["diff"] for case in reviewer_cases)
    assert all(len(case.inputs["diff"]) >= 300 for case in reviewer_cases)
    assert {case.oracle["verdict"] for case in reviewer_cases} == {"pass", "fail"}
    assert any(case.inputs["verification_passed"] is False for case in reviewer_cases)
    assert any(case.oracle["required_unrelated_paths"] for case in reviewer_cases)


def test_holdout_payload_binds_to_committed_index_when_installed() -> None:
    if not TRUSTED_HOLDOUT.exists():
        pytest.skip("Trusted holdout payload is intentionally provisioned outside Git.")
    index = load_holdout_index(DEFAULT_HOLDOUT_INDEX_PATH)
    suite = load_case_suite(TRUSTED_HOLDOUT, expected_visibility="holdout")

    validate_holdout_suite(index, suite)


def test_holdout_tamper_is_rejected(tmp_path: Path) -> None:
    index_payload = _load(DEFAULT_HOLDOUT_INDEX_PATH)
    development_payload = _load(DEFAULT_DEVELOPMENT_PATH)
    holdout_payload = deepcopy(development_payload)
    holdout_payload["suite_id"] = index_payload["suite_id"]
    holdout_payload["visibility"] = "holdout"
    holdout_payload["cases"] = holdout_payload["cases"][:4]

    index_payload["cases"] = [
        {
            "id": case["id"],
            "role": case["role"],
            "case_class": case["case_class"],
            "tags": case["tags"],
            "difficulty": case["difficulty"],
            "pattern_group": case["pattern_group"],
            "baseline_kind": case["baseline"]["kind"],
            "provenance_reference": case["provenance"]["reference"],
            "sealed_case_sha256": stable_hash(case),
        }
        for case in holdout_payload["cases"]
    ]
    index_payload["sealed_suite_sha256"] = stable_hash(holdout_payload)

    index = load_holdout_index(_write(tmp_path / "index.json", index_payload))
    suite_path = _write(tmp_path / "holdout.json", holdout_payload)
    suite = load_case_suite(suite_path, expected_visibility="holdout")
    validate_holdout_suite(index, suite)

    tampered = deepcopy(holdout_payload)
    tampered["cases"][0]["provenance"]["historical_fix"] += " Hidden mutation."
    tampered_suite = load_case_suite(
        _write(tmp_path / "tampered.json", tampered), expected_visibility="holdout"
    )
    with pytest.raises(RealTaskCorpusError, match="suite hash"):
        validate_holdout_suite(index, tampered_suite)


def test_evidence_snapshot_identity_is_verified(tmp_path: Path) -> None:
    payload = _load(DEFAULT_DEVELOPMENT_PATH)
    case = next(
        item
        for item in payload["cases"]
        if item["baseline"]["kind"] == "evidence_snapshot"
    )
    case["inputs"]["repository_evidence"].append("Unbound evidence mutation.")

    with pytest.raises(RealTaskCorpusError, match="immutable input snapshot"):
        load_case_suite(
            _write(tmp_path / "development.json", payload),
            expected_visibility="development",
        )


def test_cross_split_pattern_leakage_is_rejected(tmp_path: Path) -> None:
    development = load_case_suite(
        DEFAULT_DEVELOPMENT_PATH, expected_visibility="development"
    )
    index_payload = _load(DEFAULT_HOLDOUT_INDEX_PATH)
    index_payload["cases"][0]["pattern_group"] = development.cases[0].pattern_group
    index = load_holdout_index(_write(tmp_path / "index.json", index_payload))

    with pytest.raises(RealTaskCorpusError, match="pattern groups overlap"):
        validate_corpus(development, index)


def test_machine_specific_paths_are_rejected(tmp_path: Path) -> None:
    payload = _load(DEFAULT_DEVELOPMENT_PATH)
    payload["cases"][0]["inputs"]["repository_evidence"][
        0
    ] = "Failure occurred under /home/example/private-worktree."

    with pytest.raises(RealTaskCorpusError, match="machine-specific"):
        load_case_suite(
            _write(tmp_path / "development.json", payload),
            expected_visibility="development",
        )
