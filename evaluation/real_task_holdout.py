"""Run the one-shot Track G holdout qualification for Qwen and Qwythos."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.dspy_lm import build_dspy_lm_with_profile
from runtime.route_contract_diagnostic import (
    DiagnosticError as ReportWriteError,
    HttpClient,
    inspect_service,
    write_report,
)
from runtime.route_profiles import RouteProfile

from .outcomes import stable_hash
from .real_task_corpus import (
    CaseSuite,
    HoldoutIndex,
    load_case_suite,
    load_holdout_index,
    validate_holdout_suite,
)
from .real_task_development import ROLES, active_prompt_snapshot, git_commit, run_case
from .real_task_profile_tuning import (
    PROFILE_FIELDS,
    ProfileTuningError as BaseTuningError,
    _exact_keys,
    _integer,
    _load_json,
    _mapping,
    _material_regressions,
    _number,
    _profile_snapshot,
    _string,
    _unit_interval,
    _validate_attempt,
    summarize,
)
from .real_task_prompt_tuning import (
    PromptTuningError,
    PromptTuningProtocol,
    _program_runner,
    _require_code_baseline,
    load_protocol as load_prompt_protocol,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "track-g-holdout-qualification-v1.json"
DEFAULT_PROMPT_PROTOCOL_PATH = (
    ROOT / "profiles" / "track-g-qwythos-prompt-tuning-v1.json"
)
DEFAULT_HOLDOUT_INDEX_PATH = (
    ROOT / "evaluation" / "real_task_cases" / "holdout-v1.index.json"
)
DEFAULT_SELECTION_PATH = (
    ROOT / ".local-coder" / "real-task-evidence" / "qwythos-prompt-selection-v1.json"
)
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "real-task-evidence"
DEFAULT_RECEIPT_DIR = ROOT / ".local-coder" / "real-task-holdout" / "receipts"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
COLLECTION_KIND = "real-task-holdout-collection-v1"
COMPARISON_KIND = "real-task-holdout-qualification-v1"
RECEIPT_KIND = "real-task-holdout-consumption-v1"
STORAGE_POLICY = (
    "one-shot trusted holdout scores only; no task, input, oracle, generated field, "
    "final-answer, prompt, or reasoning text retained"
)
SUBJECTS = ("baseline", "candidate")
REPORT_FIELDS = {
    "schema_version",
    "collection_kind",
    "protocol_id",
    "protocol_sha256",
    "scoring_version",
    "subject",
    "corpus_id",
    "suite_id",
    "suite_sha256",
    "holdout_index_sha256",
    "selection_evidence",
    "environment_id",
    "implementation_commit",
    "model_file",
    "llama_alias",
    "routes",
    "route_profiles",
    "role_prompt_profiles",
    "role_prompt_profile_sha256",
    "active_prompt_state",
    "service_identity",
    "collected_at_utc",
    "attempts",
    "summary",
    "storage_policy",
    "holdout_loaded",
    "one_shot",
    "collection_sha256",
}
SELECTION_REPORT_FIELDS = {
    "schema_version",
    "comparison_kind",
    "protocol_id",
    "protocol_sha256",
    "suite_id",
    "suite_sha256",
    "environment_id",
    "implementation_commit",
    "service_identity",
    "generation_profiles",
    "report_hashes",
    "prompt_profile_summaries",
    "selected_prompt_profiles",
    "role_decisions",
    "mixed_role_projection",
    "holdout_gate",
    "qualification_claim",
    "comparison_sha256",
}


class HoldoutQualificationError(ValueError):
    """Raised when one-shot holdout controls or evidence are invalid."""


@dataclass(frozen=True)
class SelectionEvidence:
    """Development selection identity frozen before holdout access."""

    comparison_kind: str
    comparison_sha256: str
    implementation_commit: str
    prompt_protocol_id: str
    prompt_protocol_sha256: str
    selected_prompt_profiles: Mapping[str, str]
    prompt_profile_sha256: Mapping[str, str]
    open_roles: tuple[str, ...]
    report_hashes: Mapping[str, str]


@dataclass(frozen=True)
class HoldoutSubject:
    """One frozen model, route, generation, and role-prompt assignment."""

    model_file: str
    llama_alias: str
    routes: Mapping[str, str]
    route_profiles: Mapping[str, RouteProfile]
    prompt_profiles: Mapping[str, str]


@dataclass(frozen=True)
class QualificationPolicy:
    """Accuracy-first role qualification thresholds."""

    adapter_success_rate: float
    schema_rate: float
    minimum_case_score: float
    minimum_role_mean_gain: float
    minimum_case_success_delta: float
    material_regression_delta: float
    maximum_material_regressions: int


@dataclass(frozen=True)
class HoldoutProtocol:
    """Versioned one-shot holdout comparison contract."""

    raw: Mapping[str, Any]
    protocol_id: str
    scoring_version: str
    suite_id: str
    suite_sha256: str
    index_sha256: str
    attempts_per_case: int
    selection: SelectionEvidence
    subjects: Mapping[str, HoldoutSubject]
    policy: QualificationPolicy

    @property
    def sha256(self) -> str:
        return stable_hash(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "HoldoutProtocol":
        data = _mapping(value, "protocol")
        _exact_keys(
            data,
            {
                "schema_version",
                "protocol_id",
                "scoring_version",
                "attempts_per_case",
                "holdout",
                "selection_evidence",
                "subjects",
                "qualification_policy",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise HoldoutQualificationError("protocol.schema_version must be 1")
        holdout = _mapping(data["holdout"], "protocol.holdout")
        _exact_keys(
            holdout,
            {"suite_id", "suite_sha256", "index_sha256"},
            "protocol.holdout",
        )
        selection = _selection_evidence(data["selection_evidence"])
        subject_values = _mapping(data["subjects"], "protocol.subjects")
        _exact_keys(subject_values, set(SUBJECTS), "protocol.subjects")
        subjects = {
            name: _subject(subject_values[name], f"protocol.subjects.{name}")
            for name in SUBJECTS
        }
        if subjects["baseline"].prompt_profiles != {
            "planner": "code-control",
            "reviewer": "code-control",
        }:
            raise HoldoutQualificationError(
                "baseline must use code-control for both roles"
            )
        if subjects["candidate"].prompt_profiles != dict(
            selection.selected_prompt_profiles
        ):
            raise HoldoutQualificationError(
                "candidate prompt profiles must match selection evidence"
            )
        return cls(
            raw=dict(data),
            protocol_id=_string(data["protocol_id"], "protocol.protocol_id"),
            scoring_version=_string(
                data["scoring_version"], "protocol.scoring_version"
            ),
            suite_id=_string(holdout["suite_id"], "protocol.holdout.suite_id"),
            suite_sha256=_sha256(
                holdout["suite_sha256"], "protocol.holdout.suite_sha256"
            ),
            index_sha256=_sha256(
                holdout["index_sha256"], "protocol.holdout.index_sha256"
            ),
            attempts_per_case=_one_attempt(data["attempts_per_case"]),
            selection=selection,
            subjects=subjects,
            policy=_qualification_policy(data["qualification_policy"]),
        )


def _one_attempt(value: Any) -> int:
    attempts = _integer(value, "protocol.attempts_per_case", minimum=1)
    if attempts != 1:
        raise HoldoutQualificationError("protocol.attempts_per_case must be exactly 1")
    return attempts


def _sha256(value: Any, name: str) -> str:
    result = _string(value, name).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise HoldoutQualificationError(f"{name} must be a SHA-256 hex digest")
    return result


def _full_commit(value: Any, name: str) -> str:
    result = _string(value, name).lower()
    if len(result) != 40 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise HoldoutQualificationError(f"{name} must be a full Git commit hash")
    return result


def _role_strings(value: Any, name: str) -> dict[str, str]:
    data = _mapping(value, name)
    _exact_keys(data, set(ROLES), name)
    return {role: _string(data[role], f"{name}.{role}") for role in ROLES}


def _role_hashes(value: Any, name: str) -> dict[str, str]:
    data = _mapping(value, name)
    _exact_keys(data, set(ROLES), name)
    return {role: _sha256(data[role], f"{name}.{role}") for role in ROLES}


def _selection_evidence(value: Any) -> SelectionEvidence:
    data = _mapping(value, "protocol.selection_evidence")
    _exact_keys(
        data,
        {
            "comparison_kind",
            "comparison_sha256",
            "implementation_commit",
            "prompt_protocol_id",
            "prompt_protocol_sha256",
            "selected_prompt_profiles",
            "prompt_profile_sha256",
            "open_roles",
            "report_hashes",
        },
        "protocol.selection_evidence",
    )
    raw_roles = data["open_roles"]
    if not isinstance(raw_roles, list) or raw_roles != list(ROLES):
        raise HoldoutQualificationError(
            "protocol.selection_evidence.open_roles must be planner then reviewer"
        )
    hashes = _mapping(
        data["report_hashes"], "protocol.selection_evidence.report_hashes"
    )
    expected_reports = {"code-control", "evidence-completeness", "field-checklist"}
    _exact_keys(hashes, expected_reports, "protocol.selection_evidence.report_hashes")
    return SelectionEvidence(
        comparison_kind=_string(
            data["comparison_kind"], "protocol.selection_evidence.comparison_kind"
        ),
        comparison_sha256=_sha256(
            data["comparison_sha256"],
            "protocol.selection_evidence.comparison_sha256",
        ),
        implementation_commit=_full_commit(
            data["implementation_commit"],
            "protocol.selection_evidence.implementation_commit",
        ),
        prompt_protocol_id=_string(
            data["prompt_protocol_id"],
            "protocol.selection_evidence.prompt_protocol_id",
        ),
        prompt_protocol_sha256=_sha256(
            data["prompt_protocol_sha256"],
            "protocol.selection_evidence.prompt_protocol_sha256",
        ),
        selected_prompt_profiles=_role_strings(
            data["selected_prompt_profiles"],
            "protocol.selection_evidence.selected_prompt_profiles",
        ),
        prompt_profile_sha256=_role_hashes(
            data["prompt_profile_sha256"],
            "protocol.selection_evidence.prompt_profile_sha256",
        ),
        open_roles=tuple(raw_roles),
        report_hashes={
            str(key): _sha256(item, f"protocol.selection_evidence.report_hashes.{key}")
            for key, item in hashes.items()
        },
    )


def _subject(value: Any, name: str) -> HoldoutSubject:
    data = _mapping(value, name)
    _exact_keys(
        data,
        {"model_file", "llama_alias", "routes", "route_profiles", "prompt_profiles"},
        name,
    )
    llama_alias = _string(data["llama_alias"], f"{name}.llama_alias")
    routes = _role_strings(data["routes"], f"{name}.routes")
    raw_profiles = _mapping(data["route_profiles"], f"{name}.route_profiles")
    _exact_keys(raw_profiles, set(ROLES), f"{name}.route_profiles")
    profiles: dict[str, RouteProfile] = {}
    for role in ROLES:
        profile_name = f"{name}.route_profiles.{role}"
        profile_data = _mapping(raw_profiles[role], profile_name)
        _exact_keys(profile_data, PROFILE_FIELDS, profile_name)
        try:
            profile = RouteProfile(**dict(profile_data))
        except (TypeError, ValueError) as exc:
            raise HoldoutQualificationError(f"Invalid {profile_name}: {exc}") from exc
        if profile.alias != routes[role]:
            raise HoldoutQualificationError(
                f"{profile_name}.alias must match {routes[role]}"
            )
        if profile.model_alias != llama_alias:
            raise HoldoutQualificationError(
                f"{profile_name}.model_alias must match {llama_alias}"
            )
        profiles[role] = profile
    return HoldoutSubject(
        model_file=_string(data["model_file"], f"{name}.model_file"),
        llama_alias=llama_alias,
        routes=routes,
        route_profiles=profiles,
        prompt_profiles=_role_strings(
            data["prompt_profiles"], f"{name}.prompt_profiles"
        ),
    )


def _qualification_policy(value: Any) -> QualificationPolicy:
    data = _mapping(value, "protocol.qualification_policy")
    _exact_keys(
        data,
        {
            "adapter_success_rate",
            "schema_rate",
            "minimum_case_score",
            "minimum_role_mean_gain",
            "minimum_case_success_delta",
            "material_regression_delta",
            "maximum_material_regressions",
        },
        "protocol.qualification_policy",
    )
    return QualificationPolicy(
        adapter_success_rate=_unit_interval(
            data["adapter_success_rate"],
            "protocol.qualification_policy.adapter_success_rate",
        ),
        schema_rate=_unit_interval(
            data["schema_rate"], "protocol.qualification_policy.schema_rate"
        ),
        minimum_case_score=_unit_interval(
            data["minimum_case_score"],
            "protocol.qualification_policy.minimum_case_score",
        ),
        minimum_role_mean_gain=_number(
            data["minimum_role_mean_gain"],
            "protocol.qualification_policy.minimum_role_mean_gain",
        ),
        minimum_case_success_delta=_number(
            data["minimum_case_success_delta"],
            "protocol.qualification_policy.minimum_case_success_delta",
        ),
        material_regression_delta=_number(
            data["material_regression_delta"],
            "protocol.qualification_policy.material_regression_delta",
        ),
        maximum_material_regressions=_integer(
            data["maximum_material_regressions"],
            "protocol.qualification_policy.maximum_material_regressions",
        ),
    )


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> HoldoutProtocol:
    """Load and validate the frozen one-shot qualification protocol."""
    try:
        return HoldoutProtocol.from_mapping(_load_json(path, "holdout protocol"))
    except ValueError as exc:
        if isinstance(exc, HoldoutQualificationError):
            raise
        raise HoldoutQualificationError(str(exc)) from exc


def load_bound_holdout_index(
    protocol: HoldoutProtocol,
    path: Path = DEFAULT_HOLDOUT_INDEX_PATH,
) -> HoldoutIndex:
    """Validate committed holdout metadata without opening trusted content."""
    index = load_holdout_index(path)
    if index.suite_id != protocol.suite_id:
        raise HoldoutQualificationError(
            "Holdout index suite ID does not match protocol"
        )
    if index.index_hash != protocol.index_sha256:
        raise HoldoutQualificationError("Holdout index hash does not match protocol")
    if index.sealed_suite_sha256 != protocol.suite_sha256:
        raise HoldoutQualificationError("Holdout suite hash does not match protocol")
    return index


def load_holdout_bundle(
    protocol: HoldoutProtocol,
    *,
    holdout_path: Path,
    index_path: Path = DEFAULT_HOLDOUT_INDEX_PATH,
) -> tuple[HoldoutIndex, CaseSuite]:
    """Load the trusted holdout only after one-shot reservation."""
    index = load_bound_holdout_index(protocol, index_path)
    suite = load_case_suite(holdout_path, expected_visibility="holdout")
    validate_holdout_suite(index, suite)
    if len(suite.cases) != 4:
        raise HoldoutQualificationError("Holdout suite must contain exactly four cases")
    role_counts = {
        role: sum(case.role == role for case in suite.cases) for role in ROLES
    }
    if role_counts != {"planner": 2, "reviewer": 2}:
        raise HoldoutQualificationError("Holdout suite must contain two cases per role")
    return index, suite


def validate_selection_report(
    value: Any,
    *,
    protocol: HoldoutProtocol,
    prompt_protocol: PromptTuningProtocol,
) -> dict[str, Any]:
    """Validate the frozen development selection without opening holdout."""
    report = dict(_mapping(value, "selection report"))
    _exact_keys(report, SELECTION_REPORT_FIELDS, "selection report")
    comparison_hash = _sha256(
        report.pop("comparison_sha256"), "selection report.comparison_sha256"
    )
    if stable_hash(report) != comparison_hash:
        raise HoldoutQualificationError(
            "selection report comparison hash does not match"
        )
    report["comparison_sha256"] = comparison_hash
    selection = protocol.selection
    expected = {
        "schema_version": 1,
        "comparison_kind": selection.comparison_kind,
        "protocol_id": selection.prompt_protocol_id,
        "protocol_sha256": selection.prompt_protocol_sha256,
        "implementation_commit": selection.implementation_commit,
        "report_hashes": dict(selection.report_hashes),
        "selected_prompt_profiles": {
            **dict(selection.selected_prompt_profiles),
            "overall": "evidence-completeness",
        },
        "qualification_claim": None,
    }
    for field, expected_value in expected.items():
        if report[field] != expected_value:
            raise HoldoutQualificationError(
                f"selection report {field} does not match protocol"
            )
    if comparison_hash != selection.comparison_sha256:
        raise HoldoutQualificationError("selection report is not the frozen comparison")
    if prompt_protocol.protocol_id != selection.prompt_protocol_id:
        raise HoldoutQualificationError("Prompt protocol ID does not match selection")
    if prompt_protocol.sha256 != selection.prompt_protocol_sha256:
        raise HoldoutQualificationError("Prompt protocol hash does not match selection")
    holdout_gate = _mapping(report["holdout_gate"], "selection report.holdout_gate")
    if holdout_gate.get("open_roles") != list(selection.open_roles):
        raise HoldoutQualificationError("selection report does not open both roles")
    if _profile_snapshot(protocol.subjects["candidate"].route_profiles) != (
        _profile_snapshot(prompt_protocol.generation_profiles)
    ):
        raise HoldoutQualificationError(
            "Candidate generation profiles do not match selected prompt experiment"
        )
    decisions = _mapping(report["role_decisions"], "selection report.role_decisions")
    _exact_keys(decisions, set(ROLES), "selection report.role_decisions")
    for role in ROLES:
        decision = _mapping(decisions[role], f"selection report.role_decisions.{role}")
        if (
            decision.get("selected_prompt_profile")
            != selection.selected_prompt_profiles[role]
        ):
            raise HoldoutQualificationError(
                f"selection report selected {role} prompt does not match"
            )
        if decision.get("holdout_ready") is not True:
            raise HoldoutQualificationError(
                f"selection report does not mark {role} holdout-ready"
            )
        profile_id = selection.selected_prompt_profiles[role]
        try:
            profile = prompt_protocol.prompt_profiles[profile_id]
        except KeyError as exc:
            raise HoldoutQualificationError(
                f"Selected {role} prompt profile is missing"
            ) from exc
        if profile.sha256 != selection.prompt_profile_sha256[role]:
            raise HoldoutQualificationError(
                f"Selected {role} prompt profile hash does not match"
            )
    return report


def _selection_snapshot(protocol: HoldoutProtocol) -> dict[str, Any]:
    selection = protocol.selection
    return {
        "comparison_sha256": selection.comparison_sha256,
        "prompt_protocol_id": selection.prompt_protocol_id,
        "prompt_protocol_sha256": selection.prompt_protocol_sha256,
        "selected_prompt_profiles": dict(selection.selected_prompt_profiles),
        "prompt_profile_sha256": dict(selection.prompt_profile_sha256),
    }


def _default_lm_factory(route: str, profile: RouteProfile) -> Any:
    return build_dspy_lm_with_profile(route, profile)


def _instructions_for_role(
    *,
    prompt_protocol: PromptTuningProtocol,
    profile_id: str,
    role: str,
) -> str | None:
    try:
        profile = prompt_protocol.prompt_profiles[profile_id]
    except KeyError as exc:
        raise HoldoutQualificationError(
            f"Unknown prompt profile for {role}: {profile_id}"
        ) from exc
    return profile.for_role(role)


def collect_report(
    *,
    protocol: HoldoutProtocol,
    prompt_protocol: PromptTuningProtocol,
    selection_report: Mapping[str, Any],
    suite: CaseSuite,
    subject_name: str,
    environment_id: str,
    implementation_commit: str,
    service_identity: Mapping[str, Any],
    prompt_state: Mapping[str, Mapping[str, str] | None],
    lm_factory: Callable[[str, RouteProfile], Any] = _default_lm_factory,
    runner_factory: Callable[[str, str | None], Callable[..., Any]] = _program_runner,
) -> dict[str, Any]:
    """Run every sealed case exactly once for one frozen subject."""
    if subject_name not in SUBJECTS:
        raise HoldoutQualificationError(f"Unknown holdout subject: {subject_name}")
    if suite.suite_id != protocol.suite_id or suite.suite_hash != protocol.suite_sha256:
        raise HoldoutQualificationError("Trusted holdout does not match protocol")
    if selection_report["comparison_sha256"] != protocol.selection.comparison_sha256:
        raise HoldoutQualificationError("Selection evidence changed before collection")
    subject = protocol.subjects[subject_name]
    normalized_prompt_state = _require_code_baseline(prompt_state)
    lms = {
        role: lm_factory(subject.routes[role], subject.route_profiles[role])
        for role in ROLES
    }
    runners = {
        role: runner_factory(
            role,
            _instructions_for_role(
                prompt_protocol=prompt_protocol,
                profile_id=subject.prompt_profiles[role],
                role=role,
            ),
        )
        for role in ROLES
    }
    attempts = [
        run_case(
            case=case,
            attempt=1,
            route=subject.routes[case.role],
            lm=lms[case.role],
            runner=runners[case.role],
        )
        for case in suite.cases
    ]
    role_prompt_hashes = {
        role: prompt_protocol.prompt_profiles[subject.prompt_profiles[role]].sha256
        for role in ROLES
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "collection_kind": COLLECTION_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "subject": subject_name,
        "corpus_id": suite.corpus_id,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "holdout_index_sha256": protocol.index_sha256,
        "selection_evidence": _selection_snapshot(protocol),
        "environment_id": _string(environment_id, "environment_id"),
        "implementation_commit": _full_commit(
            implementation_commit, "implementation_commit"
        ),
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "routes": dict(subject.routes),
        "route_profiles": _profile_snapshot(subject.route_profiles),
        "role_prompt_profiles": dict(subject.prompt_profiles),
        "role_prompt_profile_sha256": role_prompt_hashes,
        "active_prompt_state": normalized_prompt_state,
        "service_identity": dict(service_identity),
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "attempts": attempts,
        "summary": summarize(attempts),
        "storage_policy": STORAGE_POLICY,
        "holdout_loaded": True,
        "one_shot": True,
    }
    report["collection_sha256"] = stable_hash(report)
    return report


def validate_report(
    value: Any,
    *,
    protocol: HoldoutProtocol,
    prompt_protocol: PromptTuningProtocol,
    suite: CaseSuite,
) -> dict[str, Any]:
    """Validate one bounded holdout report and recompute its summary."""
    report = dict(_mapping(value, "holdout report"))
    _exact_keys(report, REPORT_FIELDS, "holdout report")
    collection_hash = _sha256(
        report.pop("collection_sha256"), "holdout report.collection_sha256"
    )
    if stable_hash(report) != collection_hash:
        raise HoldoutQualificationError("holdout report collection hash does not match")
    report["collection_sha256"] = collection_hash
    subject_name = _string(report["subject"], "holdout report.subject")
    if subject_name not in SUBJECTS:
        raise HoldoutQualificationError("holdout report subject is unsupported")
    subject = protocol.subjects[subject_name]
    expected = {
        "schema_version": 1,
        "collection_kind": COLLECTION_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "corpus_id": suite.corpus_id,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "holdout_index_sha256": protocol.index_sha256,
        "selection_evidence": _selection_snapshot(protocol),
        "model_file": subject.model_file,
        "llama_alias": subject.llama_alias,
        "routes": dict(subject.routes),
        "route_profiles": _profile_snapshot(subject.route_profiles),
        "role_prompt_profiles": dict(subject.prompt_profiles),
        "role_prompt_profile_sha256": {
            role: prompt_protocol.prompt_profiles[subject.prompt_profiles[role]].sha256
            for role in ROLES
        },
        "storage_policy": STORAGE_POLICY,
        "holdout_loaded": True,
        "one_shot": True,
    }
    for field, expected_value in expected.items():
        if report[field] != expected_value:
            raise HoldoutQualificationError(
                f"holdout report {field} does not match protocol"
            )
    _require_code_baseline(report["active_prompt_state"])
    _string(report["environment_id"], "holdout report.environment_id")
    _full_commit(
        report["implementation_commit"], "holdout report.implementation_commit"
    )
    try:
        timestamp = datetime.fromisoformat(
            _string(report["collected_at_utc"], "holdout report.collected_at_utc")
        )
    except ValueError as exc:
        raise HoldoutQualificationError(
            "holdout report collected_at_utc is invalid"
        ) from exc
    if timestamp.tzinfo is None:
        raise HoldoutQualificationError(
            "holdout report collected_at_utc must include timezone"
        )
    identity = _mapping(report["service_identity"], "holdout report.service_identity")
    _exact_keys(
        identity,
        {
            "model_file",
            "llama_alias",
            "build_info",
            "configured_context_tokens",
            "total_slots",
        },
        "holdout report.service_identity",
    )
    if identity["model_file"] != subject.model_file:
        raise HoldoutQualificationError("holdout report service model is inconsistent")
    if identity["llama_alias"] != subject.llama_alias:
        raise HoldoutQualificationError("holdout report service alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise HoldoutQualificationError("holdout report build_info must be a string")
    _integer(identity["configured_context_tokens"], "holdout report context")
    _integer(identity["total_slots"], "holdout report slots", minimum=1)
    raw_attempts = report["attempts"]
    if not isinstance(raw_attempts, list) or len(raw_attempts) != len(suite.cases):
        raise HoldoutQualificationError("holdout report attempts have the wrong length")
    validated = [
        _validate_attempt(
            raw_attempts[index],
            case=case,
            attempt_number=1,
            route=subject.routes[case.role],
        )
        for index, case in enumerate(suite.cases)
    ]
    if report["summary"] != summarize(validated):
        raise HoldoutQualificationError(
            "holdout report summary does not match attempts"
        )
    return report


def _case_deltas(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        case_id: {
            "role": next(
                item["role"]
                for item in baseline["attempts"]
                if item["case_id"] == case_id
            ),
            "baseline_score": metrics["mean_score"],
            "candidate_score": candidate["summary"]["by_case"][case_id]["mean_score"],
            "score_delta": float(candidate["summary"]["by_case"][case_id]["mean_score"])
            - float(metrics["mean_score"]),
        }
        for case_id, metrics in baseline["summary"]["by_case"].items()
    }


def compare_reports(
    *,
    protocol: HoldoutProtocol,
    prompt_protocol: PromptTuningProtocol,
    suite: CaseSuite,
    baseline_value: Any,
    candidate_value: Any,
) -> dict[str, Any]:
    """Issue the final role-wise holdout qualification decision."""
    baseline = validate_report(
        baseline_value,
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
    )
    candidate = validate_report(
        candidate_value,
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
    )
    if baseline["subject"] != "baseline" or candidate["subject"] != "candidate":
        raise HoldoutQualificationError("Comparison requires baseline then candidate")
    for field in (
        "environment_id",
        "implementation_commit",
        "active_prompt_state",
        "suite_sha256",
        "holdout_index_sha256",
        "selection_evidence",
        "protocol_sha256",
    ):
        if baseline[field] != candidate[field]:
            raise HoldoutQualificationError(f"Holdout reports differ in {field}")
    for field in ("build_info", "configured_context_tokens", "total_slots"):
        if baseline["service_identity"][field] != candidate["service_identity"][field]:
            raise HoldoutQualificationError(
                f"Holdout reports use different service {field}"
            )
    policy = protocol.policy
    decisions: dict[str, Any] = {}
    qualified_roles: list[str] = []
    for role in ROLES:
        baseline_metrics = baseline["summary"]["by_role"][role]
        candidate_metrics = candidate["summary"]["by_role"][role]
        gain = float(candidate_metrics["mean_score"]) - float(
            baseline_metrics["mean_score"]
        )
        success_delta = float(candidate_metrics["case_success_rate"]) - float(
            baseline_metrics["case_success_rate"]
        )
        regressions = _material_regressions(
            control=baseline,
            candidate=candidate,
            role=role,
            delta=policy.material_regression_delta,
        )
        reasons: list[str] = []
        if (
            float(candidate_metrics["adapter_success_rate"])
            < policy.adapter_success_rate
        ):
            reasons.append("adapter_success_below_gate")
        if float(candidate_metrics["schema_rate"]) < policy.schema_rate:
            reasons.append("schema_rate_below_gate")
        if float(candidate_metrics["minimum_score"]) < policy.minimum_case_score:
            reasons.append("minimum_case_score_below_gate")
        if gain + 1e-12 < policy.minimum_role_mean_gain:
            reasons.append("insufficient_role_mean_gain")
        if success_delta + 1e-12 < policy.minimum_case_success_delta:
            reasons.append("case_success_regression")
        if len(regressions) > policy.maximum_material_regressions:
            reasons.append("material_case_regression")
        qualified = not reasons
        if qualified:
            qualified_roles.append(role)
        decisions[role] = {
            "baseline_mean_score": baseline_metrics["mean_score"],
            "candidate_mean_score": candidate_metrics["mean_score"],
            "mean_score_gain": gain,
            "baseline_case_success_rate": baseline_metrics["case_success_rate"],
            "candidate_case_success_rate": candidate_metrics["case_success_rate"],
            "case_success_delta": success_delta,
            "candidate_minimum_score": candidate_metrics["minimum_score"],
            "material_regressions": regressions,
            "qualified": qualified,
            "reasons": reasons,
        }
    if qualified_roles == list(ROLES):
        claim: str | None = "planner_and_reviewer"
    elif qualified_roles == ["planner"]:
        claim = "planner_only"
    elif qualified_roles == ["reviewer"]:
        claim = "reviewer_only"
    else:
        claim = None
    comparison: dict[str, Any] = {
        "schema_version": 1,
        "comparison_kind": COMPARISON_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "holdout_index_sha256": protocol.index_sha256,
        "selection_evidence": _selection_snapshot(protocol),
        "environment_id": baseline["environment_id"],
        "implementation_commit": baseline["implementation_commit"],
        "service_identity": {
            "baseline": baseline["service_identity"],
            "candidate": candidate["service_identity"],
        },
        "report_hashes": {
            "baseline": baseline["collection_sha256"],
            "candidate": candidate["collection_sha256"],
        },
        "summaries": {
            "baseline": baseline["summary"],
            "candidate": candidate["summary"],
        },
        "case_deltas": _case_deltas(baseline, candidate),
        "role_decisions": decisions,
        "qualified_roles": qualified_roles,
        "combined_qualified": qualified_roles == list(ROLES),
        "qualification_claim": claim,
        "route_activation": None,
    }
    comparison["comparison_sha256"] = stable_hash(comparison)
    return comparison


@dataclass(frozen=True)
class ServiceSubject:
    model_file: str
    llama_alias: str


def build_service_subject(
    protocol: HoldoutProtocol, subject_name: str
) -> ServiceSubject:
    subject = protocol.subjects[subject_name]
    return ServiceSubject(subject.model_file, subject.llama_alias)


def default_output_path(subject: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"{subject}-track-g-holdout-v1-{timestamp}.json"


def reservation_path(protocol: HoldoutProtocol, subject: str) -> Path:
    return DEFAULT_RECEIPT_DIR / f"{protocol.protocol_id}-{subject}-reserved.json"


def completion_path(protocol: HoldoutProtocol, subject: str) -> Path:
    return DEFAULT_RECEIPT_DIR / f"{protocol.protocol_id}-{subject}-completed.json"


def reserve_holdout_run(
    *,
    protocol: HoldoutProtocol,
    subject: str,
    environment_id: str,
    implementation_commit: str,
) -> dict[str, Any]:
    """Create an exclusive receipt before trusted holdout content is loaded."""
    if subject not in SUBJECTS:
        raise HoldoutQualificationError(f"Unknown holdout subject: {subject}")
    environment_id = _string(environment_id, "environment_id")
    implementation_commit = _full_commit(implementation_commit, "implementation_commit")
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "status": "reserved",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "suite_id": protocol.suite_id,
        "suite_sha256": protocol.suite_sha256,
        "holdout_index_sha256": protocol.index_sha256,
        "selection_comparison_sha256": protocol.selection.comparison_sha256,
        "subject": subject,
        "environment_id": environment_id,
        "implementation_commit": implementation_commit,
        "reserved_at_utc": datetime.now(UTC).isoformat(),
    }
    receipt["receipt_sha256"] = stable_hash(receipt)
    write_report(reservation_path(protocol, subject), receipt)
    return receipt


def complete_holdout_run(
    *,
    protocol: HoldoutProtocol,
    subject: str,
    reservation: Mapping[str, Any],
    report: Mapping[str, Any],
    output: Path,
) -> dict[str, Any]:
    """Write a separate completion receipt without replacing the reservation."""
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "status": "completed",
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "subject": subject,
        "reservation_sha256": reservation["receipt_sha256"],
        "collection_sha256": report["collection_sha256"],
        "report_name": output.name,
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    receipt["receipt_sha256"] = stable_hash(receipt)
    write_report(completion_path(protocol, subject), receipt)
    return receipt


def _require_completion_receipt(
    *,
    protocol: HoldoutProtocol,
    subject: str,
    report: Mapping[str, Any],
) -> None:
    reservation_value = dict(
        _mapping(
            _load_json(
                reservation_path(protocol, subject), "holdout reservation receipt"
            ),
            "reservation receipt",
        )
    )
    reservation_hash = _sha256(
        reservation_value.pop("receipt_sha256"),
        "reservation receipt.receipt_sha256",
    )
    if stable_hash(reservation_value) != reservation_hash:
        raise HoldoutQualificationError("Holdout reservation receipt hash is invalid")
    if reservation_value.get("status") != "reserved":
        raise HoldoutQualificationError("Holdout reservation receipt is inconsistent")
    if reservation_value.get("protocol_sha256") != protocol.sha256:
        raise HoldoutQualificationError("Holdout reservation protocol differs")
    if reservation_value.get("subject") != subject:
        raise HoldoutQualificationError("Holdout reservation subject differs")

    path = completion_path(protocol, subject)
    value = dict(_mapping(_load_json(path, "holdout completion receipt"), "receipt"))
    receipt_hash = _sha256(value.pop("receipt_sha256"), "receipt.receipt_sha256")
    if stable_hash(value) != receipt_hash:
        raise HoldoutQualificationError("Holdout completion receipt hash is invalid")
    if value.get("status") != "completed" or value.get("subject") != subject:
        raise HoldoutQualificationError("Holdout completion receipt is inconsistent")
    if value.get("protocol_sha256") != protocol.sha256:
        raise HoldoutQualificationError("Holdout completion receipt protocol differs")
    if value.get("reservation_sha256") != reservation_hash:
        raise HoldoutQualificationError("Holdout completion reservation differs")
    if value.get("collection_sha256") != report["collection_sha256"]:
        raise HoldoutQualificationError("Holdout completion receipt report differs")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="consume one subject holdout run")
    collect.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    collect.add_argument(
        "--prompt-protocol", type=Path, default=DEFAULT_PROMPT_PROTOCOL_PATH
    )
    collect.add_argument(
        "--selection-report", type=Path, default=DEFAULT_SELECTION_PATH
    )
    collect.add_argument("--holdout-suite", type=Path, required=True)
    collect.add_argument("--subject", choices=SUBJECTS, required=True)
    collect.add_argument("--environment-id", required=True)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)
    compare = subparsers.add_parser("compare", help="issue the final role decision")
    compare.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    compare.add_argument(
        "--prompt-protocol", type=Path, default=DEFAULT_PROMPT_PROTOCOL_PATH
    )
    compare.add_argument(
        "--selection-report", type=Path, default=DEFAULT_SELECTION_PATH
    )
    compare.add_argument("--holdout-suite", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    return result


def _collect(arguments: argparse.Namespace) -> int:
    protocol = load_protocol(arguments.protocol)
    prompt_protocol = load_prompt_protocol(arguments.prompt_protocol)
    selection_report = validate_selection_report(
        _load_json(arguments.selection_report, "prompt selection report"),
        protocol=protocol,
        prompt_protocol=prompt_protocol,
    )
    output = arguments.output or default_output_path(arguments.subject)
    if output.exists():
        raise HoldoutQualificationError(f"Refusing to overwrite report: {output}")
    if not arguments.holdout_suite.is_file():
        raise HoldoutQualificationError(
            f"Trusted holdout payload is missing: {arguments.holdout_suite}"
        )
    implementation_commit = git_commit()
    prompt_state = active_prompt_snapshot()
    _require_code_baseline(prompt_state)
    service_identity = inspect_service(
        client=HttpClient(),
        llama_base_url=arguments.llama_base_url,
        subject=build_service_subject(protocol, arguments.subject),
    )
    load_bound_holdout_index(protocol)
    reservation = reserve_holdout_run(
        protocol=protocol,
        subject=arguments.subject,
        environment_id=arguments.environment_id,
        implementation_commit=implementation_commit,
    )
    _, suite = load_holdout_bundle(
        protocol,
        holdout_path=arguments.holdout_suite,
    )
    report = collect_report(
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        selection_report=selection_report,
        suite=suite,
        subject_name=arguments.subject,
        environment_id=arguments.environment_id,
        implementation_commit=implementation_commit,
        service_identity=service_identity,
        prompt_state=prompt_state,
    )
    if active_prompt_snapshot() != prompt_state:
        raise HoldoutQualificationError("Active prompt state changed during holdout")
    validate_report(
        report,
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
    )
    write_report(output, report)
    complete_holdout_run(
        protocol=protocol,
        subject=arguments.subject,
        reservation=reservation,
        report=report,
        output=output,
    )
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


def _compare(arguments: argparse.Namespace) -> int:
    protocol = load_protocol(arguments.protocol)
    prompt_protocol = load_prompt_protocol(arguments.prompt_protocol)
    validate_selection_report(
        _load_json(arguments.selection_report, "prompt selection report"),
        protocol=protocol,
        prompt_protocol=prompt_protocol,
    )
    _, suite = load_holdout_bundle(protocol, holdout_path=arguments.holdout_suite)
    baseline = _load_json(arguments.baseline, "baseline holdout report")
    candidate = _load_json(arguments.candidate, "candidate holdout report")
    comparison = compare_reports(
        protocol=protocol,
        prompt_protocol=prompt_protocol,
        suite=suite,
        baseline_value=baseline,
        candidate_value=candidate,
    )
    _require_completion_receipt(protocol=protocol, subject="baseline", report=baseline)
    _require_completion_receipt(
        protocol=protocol, subject="candidate", report=candidate
    )
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
    raise HoldoutQualificationError(f"Unsupported command: {arguments.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        HoldoutQualificationError,
        PromptTuningError,
        BaseTuningError,
        ReportWriteError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"real-task-holdout: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
