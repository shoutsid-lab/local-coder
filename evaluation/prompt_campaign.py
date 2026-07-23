"""Typed, inert prompt-optimization campaign candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .outcomes import stable_hash

SOURCE_CAMPAIGN_KIND = "source"
PROMPT_OPTIMIZATION_CAMPAIGN_KIND = "prompt-optimization"
PROMPT_CANDIDATE_ARTIFACT_KIND = "prompt_candidate"
SUPPORTED_CAMPAIGN_KINDS = {
    SOURCE_CAMPAIGN_KIND,
    PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
}
PROMPT_CAMPAIGN_SCHEMA_VERSION = 2
PROMPT_CANDIDATE_SCHEMA_VERSION = 2


class PromptCampaignError(ValueError):
    """Raised when prompt-campaign lineage is incomplete or inconsistent."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _saved_candidate_instruction_hash(path: Path) -> str:
    """Hash predictor instructions stored in one DSPy candidate JSON file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptCampaignError("GEPA candidate JSON is unreadable.") from exc
    if not isinstance(payload, dict):
        raise PromptCampaignError("GEPA candidate JSON must be an object.")
    instructions: dict[str, str] = {}
    for name, component in payload.items():
        if name == "metadata" or not isinstance(component, dict):
            continue
        signature = component.get("signature")
        if not isinstance(signature, dict):
            continue
        instruction = signature.get("instructions")
        if isinstance(instruction, str):
            instructions[str(name)] = instruction
    if not instructions:
        raise PromptCampaignError(
            "GEPA candidate JSON contains no predictor instruction lineage."
        )
    return stable_hash(instructions)


def _validate_dataset_path(path: Path) -> Path:
    resolved = path.resolve()
    lowered = tuple(part.lower() for part in resolved.parts)
    protected_markers = (
        ("evaluation", "holdout"),
        ("evaluation", "oracles"),
        (".local-coder", "holdout"),
    )
    if any(
        lowered[index : index + len(marker)] == marker
        for marker in protected_markers
        for index in range(len(lowered) - len(marker) + 1)
    ):
        raise PromptCampaignError(
            "Prompt optimization datasets cannot use trusted holdout or oracle paths."
        )
    return resolved


@dataclass(frozen=True)
class PromptCampaignSpec:
    """Frozen GEPA inputs for one prompt-optimization campaign."""

    role: str
    dataset: str
    dataset_hash: str
    dataset_manifest_hash: str
    source_run_ids: tuple[str, ...]
    reflection_route: str
    target_metric_calls: int
    hard_model_call_limit: int
    max_unsafe_proposals: int
    no_improvement_patience: int
    reflection_max_tokens: int
    max_instruction_chars: int
    allow_perfect_only: bool
    force_search_perfect_baseline: bool
    seed: int
    num_threads: int
    schema_version: int = PROMPT_CAMPAIGN_SCHEMA_VERSION

    @classmethod
    def from_dataset(
        cls,
        dataset: Path,
        *,
        role: str,
        reflection_route: str,
        target_metric_calls: int,
        hard_model_call_limit: int,
        max_unsafe_proposals: int,
        no_improvement_patience: int,
        reflection_max_tokens: int,
        max_instruction_chars: int,
        allow_perfect_only: bool,
        force_search_perfect_baseline: bool,
        seed: int,
        num_threads: int,
    ) -> "PromptCampaignSpec":
        """Load and freeze one hash-verified exported dataset."""
        from runtime.dspy_programs.gepa_dataset import (
            GepaDatasetError,
            load_gepa_dataset,
        )

        dataset = _validate_dataset_path(dataset)
        try:
            manifest, records = load_gepa_dataset(dataset)
        except GepaDatasetError as exc:
            raise PromptCampaignError(str(exc)) from exc
        if not any(record.get("role") == role for record in records):
            raise PromptCampaignError(
                f"Dataset contains no audited examples for role {role}."
            )
        if reflection_route not in {"local-fast", "local-plan", "local-review"}:
            raise PromptCampaignError(
                f"Unsupported prompt reflection route: {reflection_route}"
            )
        positive_values = {
            "target_metric_calls": target_metric_calls,
            "hard_model_call_limit": hard_model_call_limit,
            "max_unsafe_proposals": max_unsafe_proposals,
            "no_improvement_patience": no_improvement_patience,
            "reflection_max_tokens": reflection_max_tokens,
            "max_instruction_chars": max_instruction_chars,
            "num_threads": num_threads,
        }
        invalid = [name for name, value in positive_values.items() if value <= 0]
        if invalid:
            raise PromptCampaignError(
                f"Prompt campaign values must be positive: {sorted(invalid)}"
            )
        return cls(
            role=role,
            dataset=str(dataset),
            dataset_hash=str(manifest["dataset_hash"]),
            dataset_manifest_hash=str(manifest["manifest_hash"]),
            source_run_ids=tuple(sorted(manifest.get("source_run_ids", []))),
            reflection_route=reflection_route,
            target_metric_calls=target_metric_calls,
            hard_model_call_limit=hard_model_call_limit,
            max_unsafe_proposals=max_unsafe_proposals,
            no_improvement_patience=no_improvement_patience,
            reflection_max_tokens=reflection_max_tokens,
            max_instruction_chars=max_instruction_chars,
            allow_perfect_only=allow_perfect_only,
            force_search_perfect_baseline=force_search_perfect_baseline,
            seed=seed,
            num_threads=num_threads,
        )

    @classmethod
    def from_metadata(cls, metadata: Any) -> "PromptCampaignSpec":
        """Reconstruct and validate a stored prompt campaign specification."""
        if not isinstance(metadata, dict):
            raise PromptCampaignError("Prompt campaign metadata must be an object.")
        if metadata.get("campaign_kind") != PROMPT_OPTIMIZATION_CAMPAIGN_KIND:
            raise PromptCampaignError("Brief is not a prompt-optimization brief.")
        payload = metadata.get("prompt_optimization")
        if not isinstance(payload, dict):
            raise PromptCampaignError("Prompt campaign specification is missing.")
        normalized = dict(payload)
        source_version = int(normalized.get("schema_version", 1))
        if source_version == 1:
            target = int(normalized.pop("max_metric_calls", 60))
            normalized["target_metric_calls"] = target
            normalized["hard_model_call_limit"] = target
            normalized["max_unsafe_proposals"] = 3
            normalized["schema_version"] = PROMPT_CAMPAIGN_SCHEMA_VERSION
        source_run_ids = normalized.get("source_run_ids")
        if not isinstance(source_run_ids, (list, tuple)) or not all(
            isinstance(run_id, str) and run_id for run_id in source_run_ids
        ):
            raise PromptCampaignError(
                "Prompt campaign source run lineage is malformed."
            )
        normalized["source_run_ids"] = tuple(source_run_ids)
        try:
            spec = cls(**normalized)
        except (TypeError, ValueError) as exc:
            raise PromptCampaignError(
                "Prompt campaign specification is malformed."
            ) from exc
        if spec.schema_version != PROMPT_CAMPAIGN_SCHEMA_VERSION:
            raise PromptCampaignError(
                f"Unsupported prompt campaign schema: {spec.schema_version}"
            )
        current = cls.from_dataset(
            Path(spec.dataset),
            role=spec.role,
            reflection_route=spec.reflection_route,
            target_metric_calls=spec.target_metric_calls,
            hard_model_call_limit=spec.hard_model_call_limit,
            max_unsafe_proposals=spec.max_unsafe_proposals,
            no_improvement_patience=spec.no_improvement_patience,
            reflection_max_tokens=spec.reflection_max_tokens,
            max_instruction_chars=spec.max_instruction_chars,
            allow_perfect_only=spec.allow_perfect_only,
            force_search_perfect_baseline=spec.force_search_perfect_baseline,
            seed=spec.seed,
            num_threads=spec.num_threads,
        )
        if (
            current.dataset_hash != spec.dataset_hash
            or current.dataset_manifest_hash != spec.dataset_manifest_hash
        ):
            raise PromptCampaignError(
                "Prompt campaign dataset identity changed after brief creation."
            )
        return spec

    def to_metadata(self) -> dict[str, Any]:
        """Return brief metadata that freezes all optimizer inputs."""
        return {
            "schema_version": PROMPT_CAMPAIGN_SCHEMA_VERSION,
            "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
            "prompt_optimization": asdict(self),
        }


def build_prompt_improvement_brief(
    spec: PromptCampaignSpec,
    *,
    baseline_commit: str,
    suite_hash: str,
    budget: dict[str, int],
    rollback_condition: str,
    forbidden_files: Iterable[str],
    evidence_run_ids: Iterable[str],
    evaluation_holdout: dict[str, object] | None = None,
    prompt_evaluator_hash: str | None = None,
) -> dict[str, Any]:
    """Build one inert prompt-specific improvement brief."""
    metrics = (
        {
            "measure": "gepa_development_score",
            "direction": "strict_increase",
            "role": spec.role,
        },
        {
            "measure": "offline_holdout_delta",
            "direction": "non_decrease",
            "role": spec.role,
        },
    )
    holdout = dict(
        evaluation_holdout
        or {
            "mode": "unspecified",
            "identity_hash": None,
        }
    )
    if holdout.get("mode") not in {"external", "deferred", "unspecified"}:
        raise PromptCampaignError("Unsupported prompt evaluation holdout mode.")
    if prompt_evaluator_hash is not None and not prompt_evaluator_hash.strip():
        raise PromptCampaignError("Prompt evaluator hash must be non-empty text.")
    metadata = spec.to_metadata()
    metadata["evaluation_holdout"] = holdout
    metadata["prompt_evaluator"] = {
        "mode": ("frozen" if prompt_evaluator_hash is not None else "deferred"),
        "identity_hash": prompt_evaluator_hash,
    }
    body = {
        "evidence_run_ids": tuple(sorted(set(evidence_run_ids))),
        "baseline_commit": baseline_commit,
        "failure_class": "prompt_optimization",
        "hypothesis": (
            f"A bounded GEPA candidate for {spec.role} will strictly improve "
            "development replay without reducing offline holdout score."
        ),
        "allowed_files": (),
        "forbidden_files": tuple(sorted(set(forbidden_files))),
        "acceptance_metrics": metrics,
        "suite_hash": suite_hash,
        "budget": budget,
        "rollback_condition": rollback_condition.strip(),
        "metadata": metadata,
    }
    if not body["rollback_condition"]:
        raise PromptCampaignError("A prompt campaign rollback condition is required.")
    return {"id": stable_hash(body)[:12], **body}


def _prompt_build_outcome(optimization: dict[str, Any]) -> str:
    if optimization.get("candidate_accepted") is True:
        return "candidate_ready"
    outcome = str(optimization.get("optimization_outcome") or "")
    if outcome.startswith("rejected_") or outcome == "hard_model_call_limit":
        return "candidate_rejected"
    return "no_improvement"


def build_prompt_candidate_artifact(
    spec: PromptCampaignSpec,
    output: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Validate one GEPA result and return immutable candidate lineage."""
    output = output.resolve()
    candidate_path = output / "candidate.json"
    report_path = output / "report.json"
    manifest_path = output / "manifest.json"
    if not all(path.is_file() for path in (candidate_path, report_path, manifest_path)):
        raise PromptCampaignError("GEPA output is missing required immutable files.")
    manifest = result.get("manifest")
    report = result.get("report")
    if not isinstance(manifest, dict) or not isinstance(report, dict):
        raise PromptCampaignError("GEPA result is missing manifest or report data.")
    stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored_report = json.loads(report_path.read_text(encoding="utf-8"))
    if stored_manifest != manifest or stored_report != report:
        raise PromptCampaignError("GEPA result differs from its stored output files.")
    claimed_manifest_hash = manifest.get("manifest_hash")
    manifest_hash_input = dict(manifest)
    manifest_hash_input.pop("manifest_hash", None)
    if claimed_manifest_hash != stable_hash(manifest_hash_input):
        raise PromptCampaignError("GEPA manifest hash does not match its content.")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise PromptCampaignError("GEPA manifest has no file hashes.")
    expected_hashes = {
        "candidate.json": _sha256_file(candidate_path),
        "report.json": _sha256_file(report_path),
    }
    if any(files.get(name) != value for name, value in expected_hashes.items()):
        raise PromptCampaignError("GEPA output hashes do not match the manifest.")
    if report.get("dataset_hash") != spec.dataset_hash:
        raise PromptCampaignError("GEPA report dataset does not match the brief.")
    if report.get("role") != spec.role:
        raise PromptCampaignError("GEPA report role does not match the brief.")
    if report.get("activation") != "not_performed":
        raise PromptCampaignError("Prompt candidate unexpectedly performed activation.")
    if report.get("promotion") != "not_performed":
        raise PromptCampaignError("Prompt candidate unexpectedly performed promotion.")
    optimization = report.get("optimization")
    if not isinstance(optimization, dict):
        raise PromptCampaignError("GEPA report has no optimization result.")
    instruction_hashes = optimization.get("instruction_hashes")
    if not isinstance(instruction_hashes, dict):
        raise PromptCampaignError("GEPA report has no instruction lineage hashes.")
    baseline_hash = instruction_hashes.get("baseline")
    proposed_hash = instruction_hashes.get("proposed")
    selected_hash = instruction_hashes.get("selected")
    hashes = (baseline_hash, proposed_hash, selected_hash)
    if any(not isinstance(value, str) or not value for value in hashes):
        raise PromptCampaignError("Prompt candidate instruction hashes are incomplete.")
    proposed_changed = proposed_hash != baseline_hash
    selected_changed = selected_hash != baseline_hash
    reported_proposed_changed = optimization.get("proposed_candidate_changed")
    reported_selected_changed = optimization.get("selected_candidate_changed")
    reported_candidate_changed = optimization.get("candidate_changed")
    if reported_proposed_changed is not proposed_changed:
        raise PromptCampaignError(
            "GEPA proposed-candidate change flag contradicts instruction hashes."
        )
    if reported_selected_changed is not selected_changed:
        raise PromptCampaignError(
            "GEPA selected-candidate change flag contradicts instruction hashes."
        )
    if reported_candidate_changed is not selected_changed:
        raise PromptCampaignError(
            "GEPA candidate change flag must describe the selected instruction."
        )
    accepted_value = optimization.get("candidate_accepted")
    if not isinstance(accepted_value, bool):
        raise PromptCampaignError("GEPA candidate acceptance flag is malformed.")
    accepted = accepted_value
    winning = optimization.get("winning_candidate")
    saved_instruction_hash = _saved_candidate_instruction_hash(candidate_path)
    if saved_instruction_hash != selected_hash:
        raise PromptCampaignError(
            "Saved GEPA candidate does not match the selected instruction hash."
        )
    if accepted and (not selected_changed or winning != "optimized"):
        raise PromptCampaignError(
            "Accepted prompt candidate does not select an optimized instruction."
        )
    if not accepted and (selected_changed or winning != "baseline"):
        raise PromptCampaignError(
            "Rejected prompt candidate must select the baseline instruction."
        )
    build_outcome = _prompt_build_outcome(optimization)
    artifact = {
        "schema_version": PROMPT_CANDIDATE_SCHEMA_VERSION,
        "artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND,
        "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        "role": spec.role,
        "dataset": spec.dataset,
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
        "baseline_instruction_hash": baseline_hash,
        "candidate_instruction_hash": selected_hash,
        "proposed_instruction_hash": proposed_hash,
        "gepa_manifest_hash": manifest.get("manifest_hash"),
        "gepa_candidate_hash": expected_hashes["candidate.json"],
        "gepa_report_hash": expected_hashes["report.json"],
        "optimization_outcome": optimization.get("optimization_outcome"),
        "build_outcome": build_outcome,
        "winning_candidate": winning,
        "proposed_candidate_changed": proposed_changed,
        "selected_candidate_changed": selected_changed,
        "candidate_changed": selected_changed,
        "candidate_accepted": accepted,
        "budget": report.get("budget"),
        "metric_call_accounting": report.get("metric_call_accounting"),
        "model_call_accounting": report.get("model_call_accounting"),
        "activation": report["activation"],
        "promotion": report["promotion"],
        "output": str(output),
    }
    required_text = (
        "baseline_instruction_hash",
        "candidate_instruction_hash",
        "proposed_instruction_hash",
        "gepa_manifest_hash",
        "optimization_outcome",
        "build_outcome",
        "winning_candidate",
    )
    if any(
        not isinstance(artifact[name], str) or not artifact[name]
        for name in required_text
    ):
        raise PromptCampaignError("Prompt candidate lineage is incomplete.")
    return artifact


def prompt_candidate_build_status(artifact: dict[str, Any]) -> str:
    """Return the persisted candidate-build state for one prompt artifact."""
    status = artifact.get("build_outcome")
    allowed = {"candidate_ready", "candidate_rejected", "no_improvement"}
    if status not in allowed:
        raise PromptCampaignError(f"Unsupported prompt build outcome: {status}")
    return str(status)


def load_prompt_candidate_artifact(
    build: dict[str, Any],
) -> dict[str, Any]:
    """Return one hash-verified prompt candidate artifact from a campaign build."""
    artifacts = build.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise PromptCampaignError(
            "Prompt candidate build requires exactly one candidate artifact."
        )
    stored = artifacts[0]
    try:
        artifact = json.loads(stored["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PromptCampaignError("Prompt candidate artifact is malformed.") from exc
    if not isinstance(artifact, dict):
        raise PromptCampaignError("Prompt candidate artifact must be an object.")
    if stored.get("kind") != PROMPT_CANDIDATE_ARTIFACT_KIND:
        raise PromptCampaignError("Campaign build has an unexpected artifact kind.")
    content_hash = stored.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        raise PromptCampaignError("Prompt candidate artifact hash is missing.")
    if content_hash != stable_hash(artifact):
        raise PromptCampaignError("Prompt candidate artifact hash is invalid.")
    stored_path = stored.get("path")
    output_path = artifact.get("output")
    if not isinstance(stored_path, str) or not isinstance(output_path, str):
        raise PromptCampaignError("Prompt candidate artifact path is missing.")
    if Path(stored_path).resolve() != Path(output_path).resolve():
        raise PromptCampaignError("Prompt candidate artifact path is inconsistent.")
    if artifact.get("artifact_kind") != PROMPT_CANDIDATE_ARTIFACT_KIND:
        raise PromptCampaignError("Campaign build has no prompt candidate artifact.")
    return artifact


def require_prompt_candidate_evaluation_eligibility(
    build: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed unless a campaign build contains an accepted prompt candidate."""
    status = build.get("status")
    if status != "candidate_ready":
        raise PromptCampaignError(
            f"Prompt candidate build is not evaluation-ready: {status}."
        )
    artifact = load_prompt_candidate_artifact(build)
    if artifact.get("candidate_accepted") is not True:
        raise PromptCampaignError("Prompt candidate was not accepted for evaluation.")
    if artifact.get("selected_candidate_changed") is not True:
        raise PromptCampaignError("Prompt candidate does not differ from baseline.")
    return artifact


def build_prompt_candidate(
    spec: PromptCampaignSpec,
    output: Path,
    *,
    optimizer: Callable[..., dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run bounded GEPA and return its result plus inert candidate artifact."""
    from runtime.dspy_programs.gepa_runner import (
        GepaRunnerError,
        run_gepa_optimization,
    )

    selected_optimizer = optimizer or run_gepa_optimization
    try:
        result = selected_optimizer(
            Path(spec.dataset),
            output,
            role=spec.role,
            reflection_route=spec.reflection_route,
            target_metric_calls=spec.target_metric_calls,
            hard_model_call_limit=spec.hard_model_call_limit,
            max_unsafe_proposals=spec.max_unsafe_proposals,
            no_improvement_patience=spec.no_improvement_patience,
            reflection_max_tokens=spec.reflection_max_tokens,
            max_instruction_chars=spec.max_instruction_chars,
            allow_perfect_only=spec.allow_perfect_only,
            force_search_perfect_baseline=spec.force_search_perfect_baseline,
            seed=spec.seed,
            num_threads=spec.num_threads,
        )
    except GepaRunnerError as exc:
        raise PromptCampaignError(str(exc)) from exc
    artifact = build_prompt_candidate_artifact(spec, output, result)
    return result, artifact


def prompt_candidate_content(artifact: dict[str, Any]) -> str:
    """Return canonical artifact JSON for stable storage and hashing."""
    return _canonical_json(artifact)
