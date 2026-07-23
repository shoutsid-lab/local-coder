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
PROMPT_CAMPAIGN_SCHEMA_VERSION = 1
PROMPT_CANDIDATE_SCHEMA_VERSION = 1


class PromptCampaignError(ValueError):
    """Raised when prompt-campaign lineage is incomplete or inconsistent."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


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
    max_metric_calls: int
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
        max_metric_calls: int,
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
            "max_metric_calls": max_metric_calls,
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
            max_metric_calls=max_metric_calls,
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
            max_metric_calls=spec.max_metric_calls,
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
        "metadata": spec.to_metadata(),
    }
    if not body["rollback_condition"]:
        raise PromptCampaignError("A prompt campaign rollback condition is required.")
    return {"id": stable_hash(body)[:12], **body}


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
    artifact = {
        "schema_version": PROMPT_CANDIDATE_SCHEMA_VERSION,
        "artifact_kind": PROMPT_CANDIDATE_ARTIFACT_KIND,
        "campaign_kind": PROMPT_OPTIMIZATION_CAMPAIGN_KIND,
        "role": spec.role,
        "dataset": spec.dataset,
        "dataset_hash": spec.dataset_hash,
        "dataset_manifest_hash": spec.dataset_manifest_hash,
        "baseline_instruction_hash": instruction_hashes.get("baseline"),
        "candidate_instruction_hash": instruction_hashes.get("selected"),
        "proposed_instruction_hash": instruction_hashes.get("proposed"),
        "gepa_manifest_hash": manifest.get("manifest_hash"),
        "gepa_candidate_hash": expected_hashes["candidate.json"],
        "gepa_report_hash": expected_hashes["report.json"],
        "optimization_outcome": optimization.get("optimization_outcome"),
        "winning_candidate": optimization.get("winning_candidate"),
        "candidate_changed": optimization.get("candidate_changed"),
        "candidate_accepted": optimization.get("candidate_accepted"),
        "activation": report["activation"],
        "promotion": report["promotion"],
        "output": str(output),
    }
    required_text = (
        "baseline_instruction_hash",
        "candidate_instruction_hash",
        "gepa_manifest_hash",
        "optimization_outcome",
        "winning_candidate",
    )
    if any(
        not isinstance(artifact[name], str) or not artifact[name]
        for name in required_text
    ):
        raise PromptCampaignError("Prompt candidate lineage is incomplete.")
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
            max_metric_calls=spec.max_metric_calls,
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
