"""Qualification-bound role routes, prompts, and generation profiles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .route_profiles import RouteProfile, get_route_profile

DEFAULT_ACTIVATION_PATH = Path("profiles/qwythos-role-activation-v1.json")
SUPPORTED_ROLES = {
    "explorer",
    "planner",
    "implementer",
    "repairer",
    "reviewer",
    "orchestrator",
}
QUALIFIED_ROLES = ("planner", "reviewer")
FIXED_QWEN_ROUTES = {
    "explorer": "local-plan",
    "implementer": "local-fast",
    "orchestrator": "local-plan",
    "repairer": "local-fast",
}

EXPECTED_FAST_SERVER_ARGUMENTS = (
    "--ctx-size",
    "32768",
    "--parallel",
    "1",
    "--threads",
    "6",
    "--threads-batch",
    "12",
    "--n-gpu-layers",
    "all",
    "--flash-attn",
    "on",
    "--cache-type-k",
    "q8_0",
    "--cache-type-v",
    "q8_0",
    "--ubatch-size",
    "256",
    "--metrics",
    "--cache-reuse",
    "256",
    "--slot-save-path",
    "~/llama.cpp/cache/",
)
EXPECTED_REASON_SERVER_ARGUMENTS = (
    "--ctx-size",
    "32768",
    "--parallel",
    "1",
    "--threads",
    "6",
    "--threads-batch",
    "12",
    "--n-gpu-layers",
    "all",
    "--flash-attn",
    "on",
    "--cache-type-k",
    "q8_0",
    "--cache-type-v",
    "q8_0",
    "--ubatch-size",
    "256",
    "--reasoning-format",
    "deepseek",
    "--reasoning-budget",
    "2048",
    "--spec-type",
    "draft-mtp",
    "--spec-draft-n-max",
    "2",
    "--metrics",
    "--slot-save-path",
    "~/llama.cpp/cache/",
)


class RoleProfileError(ValueError):
    """Raised when committed role activation evidence is inconsistent."""


@dataclass(frozen=True)
class QualifiedRoleProfile:
    """One role profile bound to the frozen qualification evidence."""

    role: str
    route: str
    prompt_profile: str
    instructions: str
    generation_profile: RouteProfile
    qualification_sha256: str


@dataclass(frozen=True)
class RoleActivation:
    """Validated trusted role activation state."""

    activation_id: str
    enabled: bool
    automatic_switching: bool
    role_routes: Mapping[str, str]
    fallback_role_routes: Mapping[str, str]
    qualified_profiles: Mapping[str, QualifiedRoleProfile]
    qualification_sha256: str
    model_service_config_sha256: str


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoleProfileError(f"Could not load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RoleProfileError(f"{label} must be a JSON object: {path}")
    return value


def _route_profile(value: Any, *, label: str) -> RouteProfile:
    if not isinstance(value, dict):
        raise RoleProfileError(f"{label} must be a JSON object")
    try:
        return RouteProfile(**value)
    except (TypeError, ValueError) as exc:
        raise RoleProfileError(f"{label} is invalid: {exc}") from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoleProfileError(f"{label} must be a JSON object")
    return value


def _validate_report(report: dict[str, Any], expected_hash: str) -> None:
    claimed_hash = report.get("comparison_sha256")
    if claimed_hash != expected_hash:
        raise RoleProfileError("Qualification report hash does not match activation")
    unhashed = dict(report)
    unhashed.pop("comparison_sha256", None)
    if _stable_hash(unhashed) != expected_hash:
        raise RoleProfileError("Qualification report canonical hash is invalid")
    if report.get("combined_qualified") is not True:
        raise RoleProfileError("Qualification report did not qualify both roles")
    if report.get("qualified_roles") != list(QUALIFIED_ROLES):
        raise RoleProfileError("Qualification report role set is not planner/reviewer")
    if report.get("route_activation") is not None:
        raise RoleProfileError("Qualification report unexpectedly performed activation")


def _validate_collection_report(
    report: dict[str, Any],
    *,
    subject: str,
    expected_hash: Any,
    qualification: dict[str, Any],
) -> None:
    claimed_hash = report.get("collection_sha256")
    if claimed_hash != expected_hash:
        raise RoleProfileError(f"{subject} report hash does not match qualification")
    unhashed = dict(report)
    unhashed.pop("collection_sha256", None)
    if _stable_hash(unhashed) != expected_hash:
        raise RoleProfileError(f"{subject} report canonical hash is invalid")
    if report.get("subject") != subject:
        raise RoleProfileError(f"{subject} report subject is invalid")
    for field in (
        "environment_id",
        "implementation_commit",
        "protocol_id",
        "protocol_sha256",
        "scoring_version",
        "suite_id",
        "suite_sha256",
        "holdout_index_sha256",
    ):
        if report.get(field) != qualification.get(field):
            raise RoleProfileError(f"{subject} report {field} is inconsistent")
    expected_identity = _require_mapping(
        qualification.get("service_identity"),
        "qualification.service_identity",
    ).get(subject)
    if report.get("service_identity") != expected_identity:
        raise RoleProfileError(f"{subject} service identity is inconsistent")


def _validate_protocol_hash(protocol: dict[str, Any], expected_hash: Any) -> None:
    if not isinstance(expected_hash, str) or _stable_hash(protocol) != expected_hash:
        raise RoleProfileError("Qualification protocol hash is invalid")


def _validate_model_service_policy(
    service_config: dict[str, Any],
    qualification: dict[str, Any],
) -> None:
    if service_config.get("schema_version") != 1:
        raise RoleProfileError("Model service policy schema is invalid")
    if service_config.get("host") != "127.0.0.1" or service_config.get("port") != 8080:
        raise RoleProfileError("Model service endpoint must remain 127.0.0.1:8080")
    if service_config.get("startup_timeout_seconds") != 240:
        raise RoleProfileError("Model service startup timeout must remain 240 seconds")
    if service_config.get("stop_timeout_seconds") != 20:
        raise RoleProfileError("Model service stop timeout must remain 20 seconds")
    binary = service_config.get("binary")
    if not isinstance(binary, str) or Path(binary).name != "llama-server":
        raise RoleProfileError("Model service binary must remain llama-server")

    identities = _require_mapping(
        qualification.get("service_identity"),
        "qualification.service_identity",
    )
    baseline_identity = _require_mapping(
        identities.get("baseline"),
        "qualification.service_identity.baseline",
    )
    candidate_identity = _require_mapping(
        identities.get("candidate"),
        "qualification.service_identity.candidate",
    )
    expected_build = baseline_identity.get("build_info")
    if (
        not isinstance(expected_build, str)
        or candidate_identity.get("build_info") != expected_build
        or service_config.get("build_info") != expected_build
    ):
        raise RoleProfileError("Model service llama.cpp build is inconsistent")

    profiles = _require_mapping(service_config.get("profiles"), "service profiles")
    if set(profiles) != {"fast-qwen", "reason-qwythos"}:
        raise RoleProfileError(
            "Model service profiles must remain fast-qwen/reason-qwythos"
        )
    expectations = {
        "fast-qwen": (
            baseline_identity,
            ["local-fast", "local-plan", "local-review"],
            EXPECTED_FAST_SERVER_ARGUMENTS,
        ),
        "reason-qwythos": (
            candidate_identity,
            ["local-reason"],
            EXPECTED_REASON_SERVER_ARGUMENTS,
        ),
    }
    for profile_id, (identity, routes, arguments) in expectations.items():
        profile = _require_mapping(profiles.get(profile_id), f"profiles.{profile_id}")
        if profile.get("alias") != identity.get("llama_alias"):
            raise RoleProfileError(f"{profile_id} alias differs from G4 evidence")
        model = profile.get("model")
        if not isinstance(model, str) or Path(model).name != identity.get("model_file"):
            raise RoleProfileError(f"{profile_id} model differs from G4 evidence")
        if profile.get("routes") != routes:
            raise RoleProfileError(f"{profile_id} route assignment is invalid")
        if tuple(profile.get("arguments", ())) != arguments:
            raise RoleProfileError(f"{profile_id} launch arguments are invalid")


def _resolve(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RoleProfileError(f"{label} must be a nonempty repository-relative path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RoleProfileError(f"{label} escapes the repository") from exc
    return path


@lru_cache(maxsize=8)
def load_role_activation(
    repository_root: Path | None = None,
    activation_path: Path = DEFAULT_ACTIVATION_PATH,
) -> RoleActivation:
    """Load and validate the committed planner/reviewer activation evidence."""
    root = (
        repository_root.resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    manifest_path = _resolve(root, str(activation_path), "activation_path")
    manifest = _load_json(manifest_path, "role activation manifest")
    if manifest.get("schema_version") != 1:
        raise RoleProfileError("Unsupported role activation schema")

    activation_id = manifest.get("activation_id")
    if not isinstance(activation_id, str) or not activation_id:
        raise RoleProfileError("activation_id must be a nonempty string")
    qualification_hash = manifest.get("qualification_sha256")
    if not isinstance(qualification_hash, str) or len(qualification_hash) != 64:
        raise RoleProfileError("qualification_sha256 must be a SHA-256 value")

    report_path = _resolve(
        root, manifest.get("qualification_report"), "qualification_report"
    )
    report = _load_json(report_path, "qualification report")
    _validate_report(report, qualification_hash)

    service_config_path = _resolve(
        root, manifest.get("model_service_config"), "model_service_config"
    )
    service_config = _load_json(service_config_path, "model service config")
    service_config_hash = manifest.get("model_service_config_sha256")
    if (
        not isinstance(service_config_hash, str)
        or _stable_hash(service_config) != service_config_hash
    ):
        raise RoleProfileError("Model service config hash is invalid")
    _validate_model_service_policy(service_config, report)

    supporting_paths = _require_mapping(
        manifest.get("supporting_reports"),
        "supporting_reports",
    )
    if set(supporting_paths) != {"baseline", "candidate"}:
        raise RoleProfileError("supporting_reports must define baseline and candidate")
    report_hashes = _require_mapping(report.get("report_hashes"), "report_hashes")
    for subject in ("baseline", "candidate"):
        collection_path = _resolve(
            root,
            supporting_paths.get(subject),
            f"supporting_reports.{subject}",
        )
        collection = _load_json(collection_path, f"{subject} collection report")
        _validate_collection_report(
            collection,
            subject=subject,
            expected_hash=report_hashes.get(subject),
            qualification=report,
        )

    protocol_path = _resolve(
        root, manifest.get("qualification_protocol"), "qualification_protocol"
    )
    protocol = _load_json(protocol_path, "qualification protocol")
    _validate_protocol_hash(protocol, report.get("protocol_sha256"))

    prompt_protocol_path = root / "profiles/track-g-qwythos-prompt-tuning-v1.json"
    prompt_protocol = _load_json(prompt_protocol_path, "prompt protocol")
    selection = _require_mapping(report.get("selection_evidence"), "selection_evidence")
    if _stable_hash(prompt_protocol) != selection.get("prompt_protocol_sha256"):
        raise RoleProfileError("Prompt protocol hash does not match qualification")

    role_routes = _require_mapping(manifest.get("role_routes"), "role_routes")
    if set(role_routes) != SUPPORTED_ROLES:
        raise RoleProfileError("role_routes must define every trusted runtime role")
    if any(not isinstance(route, str) for route in role_routes.values()):
        raise RoleProfileError("Every role route must be a string")
    for role, expected_route in FIXED_QWEN_ROUTES.items():
        if role_routes.get(role) != expected_route:
            raise RoleProfileError(f"Trusted {role} route must remain {expected_route}")

    fallback_routes = _require_mapping(
        manifest.get("fallback_role_routes"), "fallback_role_routes"
    )
    if set(fallback_routes) != set(QUALIFIED_ROLES):
        raise RoleProfileError("fallback_role_routes must define planner and reviewer")

    configured_roles = _require_mapping(
        manifest.get("qualified_roles"), "qualified_roles"
    )
    if set(configured_roles) != set(QUALIFIED_ROLES):
        raise RoleProfileError("qualified_roles must define planner and reviewer")

    subjects = _require_mapping(protocol.get("subjects"), "protocol.subjects")
    baseline = _require_mapping(subjects.get("baseline"), "protocol baseline")
    baseline_routes = _require_mapping(baseline.get("routes"), "baseline.routes")
    for role in QUALIFIED_ROLES:
        if fallback_routes.get(role) != baseline_routes.get(role):
            raise RoleProfileError(f"Fallback {role} route is inconsistent")
    candidate = _require_mapping(subjects.get("candidate"), "protocol candidate")
    candidate_routes = _require_mapping(candidate.get("routes"), "candidate.routes")
    candidate_prompts = _require_mapping(
        candidate.get("prompt_profiles"), "candidate.prompt_profiles"
    )
    candidate_profiles = _require_mapping(
        candidate.get("route_profiles"), "candidate.route_profiles"
    )
    prompt_profiles = _require_mapping(
        prompt_protocol.get("prompt_profiles"), "prompt_protocol.prompt_profiles"
    )
    selected_prompts = _require_mapping(
        selection.get("selected_prompt_profiles"), "selected_prompt_profiles"
    )

    profiles: dict[str, QualifiedRoleProfile] = {}
    for role in QUALIFIED_ROLES:
        configured = _require_mapping(configured_roles[role], f"qualified_roles.{role}")
        route = configured.get("route")
        prompt_profile = configured.get("prompt_profile")
        if route != role_routes[role] or route != candidate_routes.get(role):
            raise RoleProfileError(f"Qualified {role} route is inconsistent")
        if prompt_profile != candidate_prompts.get(
            role
        ) or prompt_profile != selected_prompts.get(role):
            raise RoleProfileError(f"Qualified {role} prompt is inconsistent")
        prompt_values = _require_mapping(
            prompt_profiles.get(prompt_profile),
            f"prompt_profiles.{prompt_profile}",
        )
        instructions = prompt_values.get(role)
        if not isinstance(instructions, str) or not instructions.strip():
            raise RoleProfileError(f"Qualified {role} prompt instructions are missing")
        generation_profile = _route_profile(
            candidate_profiles.get(role),
            label=f"candidate.route_profiles.{role}",
        )
        if generation_profile.alias != route:
            raise RoleProfileError(f"Qualified {role} generation route is inconsistent")
        profiles[role] = QualifiedRoleProfile(
            role=role,
            route=route,
            prompt_profile=str(prompt_profile),
            instructions=instructions,
            generation_profile=generation_profile,
            qualification_sha256=qualification_hash,
        )

    enabled = manifest.get("enabled")
    if not isinstance(enabled, bool):
        raise RoleProfileError("enabled must be a boolean")
    if manifest.get("automatic_switching") is not True:
        raise RoleProfileError("Qualified activation requires automatic switching")

    effective_routes = {str(role): str(route) for role, route in role_routes.items()}
    if not enabled:
        for role, route in fallback_routes.items():
            effective_routes[str(role)] = str(route)

    return RoleActivation(
        activation_id=activation_id,
        enabled=enabled,
        automatic_switching=True,
        role_routes=effective_routes,
        fallback_role_routes={
            str(role): str(route) for role, route in fallback_routes.items()
        },
        qualified_profiles=profiles,
        qualification_sha256=qualification_hash,
        model_service_config_sha256=service_config_hash,
    )


def role_route(role: str, *, repository_root: Path | None = None) -> str:
    """Return the trusted logical route for one runtime role."""
    activation = load_role_activation(repository_root)
    try:
        return activation.role_routes[role]
    except KeyError as exc:
        raise RoleProfileError(f"Unsupported runtime role: {role}") from exc


def role_generation_profile(
    role: str,
    *,
    repository_root: Path | None = None,
) -> RouteProfile:
    """Return the qualified role profile or the fixed route default."""
    activation = load_role_activation(repository_root)
    qualified = activation.qualified_profiles.get(role)
    if activation.enabled and qualified is not None:
        return qualified.generation_profile
    return get_route_profile(role_route(role, repository_root=repository_root))


def apply_qualified_instructions(
    program: Any,
    role: str,
    *,
    repository_root: Path | None = None,
) -> bool:
    """Apply the qualification-bound prompt to one planner or reviewer program."""
    activation = load_role_activation(repository_root)
    profile = activation.qualified_profiles.get(role)
    if not activation.enabled or profile is None:
        return False
    named = getattr(program, "named_predictors", None)
    if not callable(named):
        raise RoleProfileError(f"{role} program exposes no named predictors")
    predictors = list(named())
    if len(predictors) != 1:
        raise RoleProfileError(f"{role} program must expose exactly one predictor")
    predictor = predictors[0][1]
    signature = getattr(predictor, "signature", None)
    with_instructions = getattr(signature, "with_instructions", None)
    if not callable(with_instructions):
        raise RoleProfileError(f"{role} signature cannot accept instructions")
    predictor.signature = with_instructions(profile.instructions)
    return True


def activation_summary(repository_root: Path | None = None) -> dict[str, Any]:
    """Return a bounded operator-facing summary of active role profiles."""
    activation = load_role_activation(repository_root)
    return {
        "activation_id": activation.activation_id,
        "enabled": activation.enabled,
        "automatic_switching": activation.automatic_switching,
        "qualification_sha256": activation.qualification_sha256,
        "model_service_config_sha256": (activation.model_service_config_sha256),
        "role_routes": dict(activation.role_routes),
        "qualified_roles": {
            role: {
                "route": profile.route,
                "prompt_profile": profile.prompt_profile,
                "active": activation.enabled,
            }
            for role, profile in activation.qualified_profiles.items()
        },
    }
