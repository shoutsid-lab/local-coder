"""Collect and compare development-only Qwythos prompt-contract candidates."""

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
from .real_task_corpus import CaseSuite, load_case_suite
from .real_task_development import (
    ROLES,
    active_prompt_snapshot,
    git_commit,
    run_case,
)
from .real_task_profile_tuning import (
    ProfileTuningError as BaseTuningError,
    SelectionPolicy,
    _attempts_per_case,
    _exact_keys,
    _integer,
    _load_json,
    _mapping,
    _material_regressions,
    _normalize_prompt_state,
    _profile_eligible,
    _profile_snapshot,
    _rank,
    _route_profile,
    _selection_policy,
    _string,
    _validate_attempt,
    summarize,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = ROOT / "profiles" / "track-g-qwythos-prompt-tuning-v1.json"
DEFAULT_DEVELOPMENT_PATH = (
    ROOT / "evaluation" / "real_task_cases" / "development-v1.json"
)
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "real-task-evidence"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
COLLECTION_KIND = "real-task-prompt-tuning-v1"
COMPARISON_KIND = "real-task-prompt-selection-v1"
STORAGE_POLICY = (
    "development-only bounded scores; no generated planner/reviewer field text, "
    "final-answer text, prompt text, or reasoning text retained"
)
REPORT_FIELDS = {
    "schema_version",
    "collection_kind",
    "protocol_id",
    "protocol_sha256",
    "scoring_version",
    "prompt_profile_id",
    "prompt_profile_sha256",
    "corpus_id",
    "suite_id",
    "suite_sha256",
    "environment_id",
    "implementation_commit",
    "model_file",
    "llama_alias",
    "route",
    "generation_profiles",
    "active_prompt_state",
    "service_identity",
    "collected_at_utc",
    "attempts",
    "summary",
    "storage_policy",
    "holdout_loaded",
    "collection_sha256",
}


class PromptTuningError(ValueError):
    """Raised when prompt-tuning controls or evidence are invalid."""


@dataclass(frozen=True)
class PromptProfile:
    """One code-control or candidate instruction pair."""

    planner: str | None
    reviewer: str | None

    def for_role(self, role: str) -> str | None:
        return getattr(self, role)

    @property
    def sha256(self) -> str:
        return stable_hash({"planner": self.planner, "reviewer": self.reviewer})


@dataclass(frozen=True)
class PromptTuningProtocol:
    """Versioned development-only prompt-contract experiment."""

    raw: Mapping[str, Any]
    protocol_id: str
    scoring_version: str
    suite_id: str
    suite_sha256: str
    attempts_per_case: int
    candidate_model_file: str
    llama_alias: str
    route: str
    generation_profiles: Mapping[str, RouteProfile]
    prompt_profiles: Mapping[str, PromptProfile]
    selection_policy: SelectionPolicy

    @property
    def sha256(self) -> str:
        return stable_hash(self.raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "PromptTuningProtocol":
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
                "generation_profiles",
                "prompt_profiles",
                "selection_policy",
            },
            "protocol",
        )
        if data["schema_version"] != 1:
            raise PromptTuningError("protocol.schema_version must be 1")
        route = _string(data["route"], "protocol.route")
        if route != "local-reason":
            raise PromptTuningError("protocol.route must be local-reason")
        generation = _mapping(
            data["generation_profiles"], "protocol.generation_profiles"
        )
        _exact_keys(generation, set(ROLES), "protocol.generation_profiles")
        generation_profiles = {
            role: _route_profile(
                generation[role], f"protocol.generation_profiles.{role}", route
            )
            for role in ROLES
        }
        prompt_values = _mapping(data["prompt_profiles"], "protocol.prompt_profiles")
        expected = {"code-control", "evidence-completeness", "field-checklist"}
        _exact_keys(prompt_values, expected, "protocol.prompt_profiles")
        prompts: dict[str, PromptProfile] = {}
        for profile_id, raw_profile in prompt_values.items():
            role_values = _mapping(
                raw_profile, f"protocol.prompt_profiles.{profile_id}"
            )
            _exact_keys(
                role_values,
                set(ROLES),
                f"protocol.prompt_profiles.{profile_id}",
            )
            normalized: dict[str, str | None] = {}
            for role in ROLES:
                instruction = role_values[role]
                if instruction is None:
                    normalized[role] = None
                    continue
                text = _string(
                    instruction,
                    f"protocol.prompt_profiles.{profile_id}.{role}",
                )
                if len(text) > 1600:
                    raise PromptTuningError(
                        f"prompt profile {profile_id}.{role} exceeds 1600 characters"
                    )
                normalized[role] = text
            prompts[str(profile_id)] = PromptProfile(**normalized)
        if prompts["code-control"].planner is not None or (
            prompts["code-control"].reviewer is not None
        ):
            raise PromptTuningError("code-control must use code-defined instructions")
        selection_profiles = {profile_id: generation_profiles for profile_id in prompts}
        selection = _selection_policy(data["selection_policy"], selection_profiles)
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
            generation_profiles=generation_profiles,
            prompt_profiles=prompts,
            selection_policy=selection,
        )


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> PromptTuningProtocol:
    """Load and validate the frozen prompt-tuning protocol."""
    try:
        return PromptTuningProtocol.from_mapping(_load_json(path, "prompt protocol"))
    except ValueError as exc:
        if isinstance(exc, PromptTuningError):
            raise
        raise PromptTuningError(str(exc)) from exc


def load_development_suite(protocol: PromptTuningProtocol) -> CaseSuite:
    """Load only the committed development suite and bind its hash."""
    suite = load_case_suite(
        DEFAULT_DEVELOPMENT_PATH,
        expected_visibility="development",
    )
    if suite.suite_id != protocol.suite_id or suite.suite_hash != protocol.suite_sha256:
        raise PromptTuningError("Development suite does not match prompt protocol")
    return suite


def _set_program_instructions(program: Any, instructions: str) -> None:
    named = getattr(program, "named_predictors", None)
    if not callable(named):
        raise PromptTuningError("DSPy program exposes no named predictors")
    predictors = list(named())
    if len(predictors) != 1:
        raise PromptTuningError("Prompt tuning requires exactly one predictor per role")
    predictor = predictors[0][1]
    signature = getattr(predictor, "signature", None)
    method = getattr(signature, "with_instructions", None)
    if not callable(method):
        raise PromptTuningError("DSPy signature cannot accept instruction overrides")
    predictor.signature = method(instructions)


def _program_runner(
    role: str,
    instructions: str | None,
    *,
    dspy_module: Any | None = None,
    program_factory: Callable[[], Any] | None = None,
) -> Callable[..., Any]:
    """Build a production-equivalent JSONAdapter runner with an inert override."""

    def run(*, lm: Any, **inputs: Any) -> Any:
        nonlocal dspy_module, program_factory
        if dspy_module is None:
            try:
                import dspy as dspy_module
            except ImportError as exc:
                raise PromptTuningError(
                    "DSPy is not installed. Run `make agent-install`."
                ) from exc
        if program_factory is None:
            if role == "planner":
                from runtime.dspy_programs.planner import PlannerProgram

                factory = PlannerProgram
            elif role == "reviewer":
                from runtime.dspy_programs.reviewer import ReviewerProgram

                factory = ReviewerProgram
            else:
                raise PromptTuningError(f"Unsupported prompt-tuning role: {role}")
        else:
            factory = program_factory
        program = factory()
        if instructions is not None:
            _set_program_instructions(program, instructions)
        with dspy_module.context(
            lm=lm,
            adapter=dspy_module.JSONAdapter(),
            track_usage=True,
        ):
            return program(**inputs)

    return run


def _default_lm_factory(route: str, profile: RouteProfile) -> Any:
    return build_dspy_lm_with_profile(route, profile)


def _require_code_baseline(
    state: Mapping[str, Mapping[str, str] | None],
) -> dict[str, Mapping[str, str] | None]:
    normalized = _normalize_prompt_state(state, "active_prompt_state")
    if any(normalized[role] is not None for role in ROLES):
        raise PromptTuningError(
            "Prompt tuning requires no active deployed planner/reviewer prompt state"
        )
    return normalized


def collect_report(
    *,
    protocol: PromptTuningProtocol,
    suite: CaseSuite,
    prompt_profile_id: str,
    environment_id: str,
    implementation_commit: str,
    service_identity: Mapping[str, Any],
    prompt_state: Mapping[str, Mapping[str, str] | None],
    lm_factory: Callable[[str, RouteProfile], Any] = _default_lm_factory,
    runner_factory: Callable[[str, str | None], Callable[..., Any]] = _program_runner,
) -> dict[str, Any]:
    """Run one frozen instruction pair over every visible case twice."""
    try:
        prompt_profile = protocol.prompt_profiles[prompt_profile_id]
    except KeyError as exc:
        raise PromptTuningError(f"Unknown prompt profile: {prompt_profile_id}") from exc
    if suite.suite_id != protocol.suite_id or suite.suite_hash != protocol.suite_sha256:
        raise PromptTuningError("Development suite does not match prompt protocol")
    normalized_prompt_state = _require_code_baseline(prompt_state)
    lms = {
        role: lm_factory(protocol.route, protocol.generation_profiles[role])
        for role in ROLES
    }
    runners = {
        role: runner_factory(role, prompt_profile.for_role(role)) for role in ROLES
    }
    attempts = [
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
        "prompt_profile_id": prompt_profile_id,
        "prompt_profile_sha256": prompt_profile.sha256,
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
        "generation_profiles": _profile_snapshot(protocol.generation_profiles),
        "active_prompt_state": normalized_prompt_state,
        "service_identity": dict(service_identity),
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "attempts": attempts,
        "summary": summarize(attempts),
        "storage_policy": STORAGE_POLICY,
        "holdout_loaded": False,
    }
    report["collection_sha256"] = stable_hash(report)
    return report


def validate_report(
    value: Any,
    *,
    protocol: PromptTuningProtocol,
    suite: CaseSuite,
) -> dict[str, Any]:
    """Recompute all bounded prompt evidence and reject drift or tampering."""
    report = dict(_mapping(value, "report"))
    _exact_keys(report, REPORT_FIELDS, "report")
    collection_hash = _string(
        report.pop("collection_sha256"), "report.collection_sha256"
    )
    if stable_hash(report) != collection_hash:
        raise PromptTuningError("report.collection_sha256 does not match")
    report["collection_sha256"] = collection_hash
    expected = {
        "schema_version": 1,
        "collection_kind": COLLECTION_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "scoring_version": protocol.scoring_version,
        "corpus_id": suite.corpus_id,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "model_file": protocol.candidate_model_file,
        "llama_alias": protocol.llama_alias,
        "route": protocol.route,
        "storage_policy": STORAGE_POLICY,
        "holdout_loaded": False,
    }
    for field, expected_value in expected.items():
        if report[field] != expected_value:
            raise PromptTuningError(f"report.{field} does not match protocol")
    profile_id = _string(report["prompt_profile_id"], "report.prompt_profile_id")
    try:
        profile = protocol.prompt_profiles[profile_id]
    except KeyError as exc:
        raise PromptTuningError("report prompt profile is unknown") from exc
    if report["prompt_profile_sha256"] != profile.sha256:
        raise PromptTuningError("report prompt profile hash does not match")
    if report["generation_profiles"] != _profile_snapshot(protocol.generation_profiles):
        raise PromptTuningError("report generation profiles do not match")
    _require_code_baseline(report["active_prompt_state"])
    _string(report["environment_id"], "report.environment_id")
    commit = _string(report["implementation_commit"], "report.implementation_commit")
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit.lower()
    ):
        raise PromptTuningError("report.implementation_commit must be full hash")
    try:
        timestamp = datetime.fromisoformat(
            _string(report["collected_at_utc"], "report.collected_at_utc")
        )
    except ValueError as exc:
        raise PromptTuningError("report.collected_at_utc is invalid") from exc
    if timestamp.tzinfo is None:
        raise PromptTuningError("report.collected_at_utc must include timezone")
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
        raise PromptTuningError("report service model is inconsistent")
    if identity["llama_alias"] != protocol.llama_alias:
        raise PromptTuningError("report service alias is inconsistent")
    if not isinstance(identity["build_info"], str):
        raise PromptTuningError("report build_info must be a string")
    _integer(identity["configured_context_tokens"], "report configured context")
    _integer(identity["total_slots"], "report total slots", minimum=1)
    raw_attempts = report["attempts"]
    if not isinstance(raw_attempts, list):
        raise PromptTuningError("report.attempts must be a list")
    expected_count = len(suite.cases) * protocol.attempts_per_case
    if len(raw_attempts) != expected_count:
        raise PromptTuningError("report.attempts has the wrong length")
    validated: list[dict[str, Any]] = []
    offset = 0
    for case in suite.cases:
        for attempt_number in range(1, protocol.attempts_per_case + 1):
            validated.append(
                _validate_attempt(
                    raw_attempts[offset],
                    case=case,
                    attempt_number=attempt_number,
                    route=protocol.route,
                )
            )
            offset += 1
    if report["summary"] != summarize(validated):
        raise PromptTuningError("report.summary does not match attempts")
    return report


def compare_reports(
    values: Sequence[Any],
    *,
    protocol: PromptTuningProtocol,
    suite: CaseSuite,
) -> dict[str, Any]:
    """Select prompt candidates by frozen accuracy-first rules."""
    validated = [
        validate_report(value, protocol=protocol, suite=suite) for value in values
    ]
    by_profile = {str(report["prompt_profile_id"]): report for report in validated}
    if len(by_profile) != len(validated):
        raise PromptTuningError("Comparison contains duplicate prompt reports")
    if set(by_profile) != set(protocol.prompt_profiles):
        missing = sorted(set(protocol.prompt_profiles) - set(by_profile))
        extra = sorted(set(by_profile) - set(protocol.prompt_profiles))
        raise PromptTuningError(
            "Comparison requires every frozen prompt profile; "
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
            "generation_profiles",
        ):
            if report[field] != first[field]:
                raise PromptTuningError(f"Comparison reports differ in {field}")
    gates = protocol.selection_policy.hard_gates
    selected: dict[str, str] = {}
    for role in ROLES:
        eligible = [
            profile_id
            for profile_id, report in by_profile.items()
            if _profile_eligible(report["summary"]["by_role"][role], gates)
        ]
        if not eligible:
            raise PromptTuningError(f"No {role} prompt satisfies hard gates")
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
        raise PromptTuningError("No prompt profile satisfies overall hard gates")
    selected["overall"] = max(
        overall_eligible,
        key=lambda profile_id: _rank(by_profile[profile_id]["summary"]["overall"]),
    )
    control_id = protocol.selection_policy.control_profile
    control = by_profile[control_id]
    gate = protocol.selection_policy.holdout_gate
    selected_records: list[Mapping[str, Any]] = []
    role_decisions: dict[str, Any] = {}
    open_roles: list[str] = []
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
            delta=gate.material_regression_delta,
        )
        gain = float(chosen_metrics["mean_score"]) - float(
            control_metrics["mean_score"]
        )
        reasons: list[str] = []
        if gain + 1e-12 < gate.minimum_role_mean_gain:
            reasons.append("insufficient_role_mean_gain")
        if len(regressions) > gate.maximum_material_regressions:
            reasons.append("material_case_regression")
        ready = not reasons
        if ready:
            open_roles.append(role)
        role_decisions[role] = {
            "selected_prompt_profile": selected[role],
            "control_prompt_profile": control_id,
            "control_mean_score": control_metrics["mean_score"],
            "selected_mean_score": chosen_metrics["mean_score"],
            "mean_score_gain": gain,
            "material_regressions": regressions,
            "holdout_ready": ready,
            "reasons": reasons,
        }
    mixed = summarize(selected_records)
    combined_reasons: list[str] = []
    if float(mixed["overall"]["mean_score"]) < gate.minimum_overall_mean_score:
        combined_reasons.append("insufficient_overall_mean_score")
    if float(mixed["overall"]["stable_case_success_rate"]) < (
        gate.minimum_stable_case_success_rate
    ):
        combined_reasons.append("insufficient_stable_case_success_rate")
    if set(open_roles) != set(ROLES):
        combined_reasons.append("one_or_more_roles_not_ready")
    comparison: dict[str, Any] = {
        "schema_version": 1,
        "comparison_kind": COMPARISON_KIND,
        "protocol_id": protocol.protocol_id,
        "protocol_sha256": protocol.sha256,
        "suite_id": suite.suite_id,
        "suite_sha256": suite.suite_hash,
        "environment_id": first["environment_id"],
        "implementation_commit": first["implementation_commit"],
        "service_identity": first["service_identity"],
        "generation_profiles": first["generation_profiles"],
        "report_hashes": {
            profile_id: report["collection_sha256"]
            for profile_id, report in sorted(by_profile.items())
        },
        "prompt_profile_summaries": {
            profile_id: report["summary"]
            for profile_id, report in sorted(by_profile.items())
        },
        "selected_prompt_profiles": selected,
        "role_decisions": role_decisions,
        "mixed_role_projection": mixed,
        "holdout_gate": {
            "open_roles": open_roles,
            "combined_ready": not combined_reasons,
            "combined_reasons": combined_reasons,
        },
        "qualification_claim": None,
    }
    comparison["comparison_sha256"] = stable_hash(comparison)
    return comparison


def default_output_path(prompt_profile_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / (
        f"{prompt_profile_id}-track-g-prompt-tuning-v1-{timestamp}.json"
    )


@dataclass(frozen=True)
class ServiceSubject:
    model_file: str
    llama_alias: str


def build_service_subject(protocol: PromptTuningProtocol) -> ServiceSubject:
    return ServiceSubject(
        model_file=protocol.candidate_model_file,
        llama_alias=protocol.llama_alias,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="collect one prompt profile")
    collect.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    collect.add_argument("--prompt-profile", required=True)
    collect.add_argument("--environment-id", required=True)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--llama-base-url", default=DEFAULT_LLAMA_BASE_URL)
    compare = subparsers.add_parser("compare", help="rank all prompt profiles")
    compare.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    compare.add_argument("--reports", type=Path, nargs="+", required=True)
    compare.add_argument("--output", type=Path)
    return result


def _collect(arguments: argparse.Namespace) -> int:
    protocol = load_protocol(arguments.protocol)
    suite = load_development_suite(protocol)
    if arguments.prompt_profile not in protocol.prompt_profiles:
        raise PromptTuningError(f"Unknown prompt profile: {arguments.prompt_profile}")
    output = arguments.output or default_output_path(arguments.prompt_profile)
    prompt_state = active_prompt_snapshot()
    report = collect_report(
        protocol=protocol,
        suite=suite,
        prompt_profile_id=arguments.prompt_profile,
        environment_id=arguments.environment_id,
        implementation_commit=git_commit(),
        service_identity=inspect_service(
            client=HttpClient(),
            llama_base_url=arguments.llama_base_url,
            subject=build_service_subject(protocol),
        ),
        prompt_state=prompt_state,
    )
    if active_prompt_snapshot() != prompt_state:
        raise PromptTuningError("Active prompt state changed during collection")
    validate_report(report, protocol=protocol, suite=suite)
    write_report(output, report)
    print(
        json.dumps(
            {
                "output": str(output),
                "collection_sha256": report["collection_sha256"],
                "prompt_profile_id": report["prompt_profile_id"],
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
    suite = load_development_suite(protocol)
    reports = [_load_json(path, "prompt-tuning report") for path in arguments.reports]
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
    raise PromptTuningError(f"Unsupported command: {arguments.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        PromptTuningError,
        BaseTuningError,
        ReportWriteError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"real-task-prompt-tuning: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
