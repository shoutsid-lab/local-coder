"""Deterministic offline GEPA dataset export from audited DSPy traces."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from evaluation.outcomes import normalize_run, stable_hash
from runtime.dspy_trace import DSPY_TRACE_ARTIFACT_KIND, DSPY_TRACE_SCHEMA_VERSION
from runtime.state import StateStore

DATASET_SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 512_000
SPLIT_NAMES = ("train", "dev", "holdout")
ROLE_PROGRAMS = {
    "explorer": "ExplorerProgram",
    "planner": "PlannerProgram",
    "implementer": "ImplementerProgram",
    "repairer": "RepairerProgram",
    "reviewer": "ReviewerProgram",
}
ROLE_ROUTES = {
    "explorer": "local-plan",
    "planner": "local-plan",
    "implementer": "local-fast",
    "repairer": "local-fast",
    "reviewer": "local-review",
}
PROTECTED_MARKERS = (
    "evaluation/holdout/",
    "evaluation/oracles/",
    ".local-coder/holdout/",
)


class GepaDatasetError(ValueError):
    """Raised when audited source data cannot produce a trusted export."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise GepaDatasetError(f"{label} is not JSON text.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GepaDatasetError(f"{label} is malformed JSON.") from exc
    if not isinstance(decoded, dict):
        raise GepaDatasetError(f"{label} must contain a JSON object.")
    return decoded


def _contains_protected_material(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.replace("\\", "/").lower()
        return any(marker in lowered for marker in PROTECTED_MARKERS)
    if isinstance(value, Mapping):
        return any(
            _contains_protected_material(key) or _contains_protected_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_protected_material(item) for item in value)
    return False


def _review_outcome(details: Mapping[str, Any]) -> tuple[str, str]:
    artifacts = [
        row for row in details.get("artifacts", []) if row.get("kind") == "review"
    ]
    if not artifacts:
        raise GepaDatasetError("run has no structured reviewer artifact.")
    review = _json_object(artifacts[-1].get("content"), label="review artifact")
    verdict = review.get("verdict")
    summary = review.get("summary")
    issues = review.get("issues")
    unrelated = review.get("unrelated_changes")
    if verdict not in {"pass", "fail", "needs_attention"}:
        raise GepaDatasetError("review artifact has an invalid verdict.")
    if not isinstance(summary, str) or not summary.strip():
        raise GepaDatasetError("review artifact has no textual summary.")
    if not isinstance(issues, list) or not all(
        isinstance(item, str) for item in issues
    ):
        raise GepaDatasetError("review artifact issues are malformed.")
    if not isinstance(unrelated, list) or not all(
        isinstance(item, str) for item in unrelated
    ):
        raise GepaDatasetError("review artifact unrelated changes are malformed.")
    feedback = [f"Verdict: {verdict}", summary.strip()]
    feedback.extend(f"Issue: {item.strip()}" for item in issues if item.strip())
    feedback.extend(
        f"Unrelated change: {item.strip()}" for item in unrelated if item.strip()
    )
    return verdict, "\n".join(feedback)


def _verification_outcome(details: Mapping[str, Any]) -> tuple[bool, str]:
    rows = list(details.get("verification", []))
    if not rows:
        raise GepaDatasetError("run has no deterministic verification result.")
    latest = rows[-1]
    passed = latest.get("passed")
    output = latest.get("output")
    if passed not in {0, 1, False, True}:
        raise GepaDatasetError("verification result is malformed.")
    if not isinstance(output, str):
        raise GepaDatasetError("verification output is malformed.")
    return bool(passed), output


def _successful_backend_markers(
    details: Mapping[str, Any],
) -> set[tuple[str, str, str]]:
    markers: set[tuple[str, str, str]] = set()
    for row in details.get("model_metrics", []):
        metadata_text = row.get("metadata")
        if not isinstance(metadata_text, str):
            continue
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict):
            continue
        role_source = metadata.get("source")
        program = metadata.get("program")
        adapter = metadata.get("adapter")
        status = metadata.get("status")
        route = row.get("route")
        if (
            isinstance(role_source, str)
            and role_source.startswith("dspy-")
            and isinstance(program, str)
            and adapter == "JSONAdapter"
            and isinstance(route, str)
            and status in {"success", "repair_verification_failed"}
        ):
            markers.add((role_source.removeprefix("dspy-"), program, route))
    return markers


def _split_for_task(task: str) -> str:
    bucket = int(hashlib.sha256(task.encode("utf-8")).hexdigest()[:8], 16) % 10
    if bucket < 8:
        return "train"
    if bucket == 8:
        return "dev"
    return "holdout"


def _string_list(value: Any, *, label: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GepaDatasetError(f"DSPy trace {label} must be a list of strings.")
    if not allow_empty and not value:
        raise GepaDatasetError(f"DSPy trace {label} cannot be empty.")
    return value


def _validate_edits(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise GepaDatasetError("DSPy trace edits must contain one to eight items.")
    for edit in value:
        if not isinstance(edit, dict):
            raise GepaDatasetError("DSPy trace edit is not an object.")
        if set(edit) != {"path", "old_text", "new_text"}:
            raise GepaDatasetError("DSPy trace edit fields are malformed.")
        if not all(isinstance(edit[field], str) for field in edit):
            raise GepaDatasetError("DSPy trace edit values must be strings.")
        if not edit["path"].strip() or not edit["old_text"]:
            raise GepaDatasetError("DSPy trace edit path or old_text is empty.")
        if edit["old_text"] == edit["new_text"]:
            raise GepaDatasetError("DSPy trace edit is a no-op.")


def _validate_role_payload(trace: Mapping[str, Any], authoritative_task: str) -> None:
    role = str(trace["role"])
    inputs = trace["inputs"]
    output = trace["output"]
    trace_task = inputs.get("task")
    if not isinstance(trace_task, str) or authoritative_task.strip() not in trace_task:
        raise GepaDatasetError("DSPy trace task does not match its audited run.")

    if role in {"explorer", "planner"}:
        if not isinstance(inputs.get("delegated_task"), str):
            raise GepaDatasetError("DSPy trace delegated task is malformed.")
        _string_list(
            inputs.get("repository_evidence"),
            label="repository_evidence",
            allow_empty=False,
        )
    elif role == "implementer":
        if not isinstance(inputs.get("instruction"), str):
            raise GepaDatasetError(
                "DSPy trace implementation instruction is malformed."
            )
        _string_list(
            inputs.get("editable_files"), label="editable_files", allow_empty=False
        )
        _string_list(
            inputs.get("file_contents"), label="file_contents", allow_empty=False
        )
    elif role == "repairer":
        for field in ("delegated_task", "verification_output", "diff"):
            if not isinstance(inputs.get(field), str):
                raise GepaDatasetError(f"DSPy trace {field} is malformed.")
        _string_list(
            inputs.get("editable_files"), label="editable_files", allow_empty=False
        )
        _string_list(
            inputs.get("file_contents"), label="file_contents", allow_empty=False
        )
    elif role == "reviewer":
        if not isinstance(inputs.get("verification_passed"), bool):
            raise GepaDatasetError("DSPy trace verification flag is malformed.")
        for field in ("verification_output", "diff"):
            if not isinstance(inputs.get(field), str):
                raise GepaDatasetError(f"DSPy trace {field} is malformed.")
        _string_list(inputs.get("changed_files"), label="changed_files")

    if role == "explorer":
        _string_list(output.get("findings"), label="findings", allow_empty=False)
        _string_list(output.get("relevant_files"), label="relevant_files")
        _string_list(output.get("constraints"), label="constraints")
        _string_list(output.get("unresolved_questions"), label="unresolved_questions")
    elif role == "planner":
        if (
            not isinstance(output.get("instruction"), str)
            or not output["instruction"].strip()
        ):
            raise GepaDatasetError("DSPy trace planner instruction is malformed.")
        editable_files = _string_list(
            output.get("editable_files"), label="editable_files", allow_empty=False
        )
        if len(editable_files) > 2:
            raise GepaDatasetError("DSPy trace planner scope exceeds two files.")
        _string_list(
            output.get("acceptance_criteria"),
            label="acceptance_criteria",
            allow_empty=False,
        )
        _string_list(output.get("depends_on"), label="depends_on")
    elif role == "implementer":
        _validate_edits(output.get("edits"))
    elif role == "repairer":
        if (
            not isinstance(output.get("diagnosis"), str)
            or not output["diagnosis"].strip()
        ):
            raise GepaDatasetError("DSPy trace repair diagnosis is malformed.")
        _validate_edits(output.get("edits"))
    elif role == "reviewer":
        verdict = output.get("verdict")
        if verdict not in {"pass", "fail", "needs_attention"}:
            raise GepaDatasetError("DSPy trace reviewer verdict is malformed.")
        if not isinstance(output.get("summary"), str) or not output["summary"].strip():
            raise GepaDatasetError("DSPy trace reviewer summary is malformed.")
        _string_list(output.get("issues"), label="issues")
        _string_list(output.get("unrelated_changes"), label="unrelated_changes")


def _validated_trace(
    artifact: Mapping[str, Any],
    *,
    backend_markers: set[tuple[str, str, str]],
    authoritative_task: str,
) -> dict[str, Any]:
    content = artifact.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TRACE_BYTES:
        raise GepaDatasetError("DSPy trace is missing or exceeds the size limit.")
    trace = _json_object(content, label="DSPy trace")
    if trace.get("schema_version") != DSPY_TRACE_SCHEMA_VERSION:
        raise GepaDatasetError("DSPy trace schema version is unsupported.")
    role = trace.get("role")
    if role not in ROLE_PROGRAMS:
        raise GepaDatasetError("DSPy trace role is unsupported.")
    program = trace.get("program")
    route = trace.get("route")
    if program != ROLE_PROGRAMS[role] or route != ROLE_ROUTES[role]:
        raise GepaDatasetError("DSPy trace program or route does not match its role.")
    if trace.get("adapter") != "JSONAdapter":
        raise GepaDatasetError("DSPy trace adapter is unsupported.")
    if (role, program, route) not in backend_markers:
        raise GepaDatasetError("DSPy trace lacks a matching successful backend audit.")
    if not isinstance(trace.get("inputs"), dict) or not isinstance(
        trace.get("output"), dict
    ):
        raise GepaDatasetError("DSPy trace inputs or output are malformed.")
    _validate_role_payload(trace, authoritative_task)
    return trace


def build_gepa_examples(
    records: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build deterministic dataset records from complete audited runs."""
    examples: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    semantic_hashes: set[str] = set()
    for details in records:
        run_id = str(details.get("id", ""))
        task = details.get("task")
        if not run_id or not isinstance(task, str) or not task.strip():
            exclusions["malformed_run"] += 1
            continue
        try:
            verification_passed, verification_output = _verification_outcome(details)
            reviewer_verdict, reviewer_feedback = _review_outcome(details)
            normalized = normalize_run(dict(details))
        except GepaDatasetError as exc:
            exclusions[str(exc)] += 1
            continue
        backend_markers = _successful_backend_markers(details)
        traces = [
            row
            for row in details.get("artifacts", [])
            if row.get("kind") == DSPY_TRACE_ARTIFACT_KIND
        ]
        if not traces:
            exclusions["run has no DSPy trace artifacts."] += 1
            continue
        for artifact in traces:
            try:
                trace = _validated_trace(
                    artifact,
                    backend_markers=backend_markers,
                    authoritative_task=task,
                )
                protected_payload = {
                    "trace": trace,
                    "verification_output": verification_output,
                    "reviewer_feedback": reviewer_feedback,
                }
                if _contains_protected_material(protected_payload):
                    raise GepaDatasetError(
                        "example references protected holdout or oracle material."
                    )
            except GepaDatasetError as exc:
                exclusions[str(exc)] += 1
                continue
            semantic_hash = stable_hash(
                {
                    "run_id": run_id,
                    "role": trace["role"],
                    "program": trace["program"],
                    "output": trace["output"],
                }
            )
            if semantic_hash in semantic_hashes:
                exclusions["duplicate semantic trace"] += 1
                continue
            semantic_hashes.add(semantic_hash)
            artifact_id = artifact.get("id")
            trace_hash = stable_hash(trace)
            example_id = stable_hash(
                {
                    "run_id": run_id,
                    "artifact_id": artifact_id,
                    "trace_hash": trace_hash,
                }
            )
            examples.append(
                {
                    "schema_version": DATASET_SCHEMA_VERSION,
                    "example_id": example_id,
                    "run_id": run_id,
                    "role": trace["role"],
                    "program": trace["program"],
                    "route": trace["route"],
                    "split": _split_for_task(task),
                    "task": task.strip(),
                    "inputs": trace["inputs"],
                    "output": trace["output"],
                    "outcome": {
                        "run_status": str(details.get("status", "unknown")),
                        "verification_passed": verification_passed,
                        "verification_output": verification_output,
                        "reviewer_verdict": reviewer_verdict,
                        "reviewer_feedback": reviewer_feedback,
                        "score": float(
                            verification_passed and reviewer_verdict == "pass"
                        ),
                    },
                    "normalized_outcome": {
                        "schema_version": normalized.schema_version,
                        "task_hash": normalized.task_hash,
                        "baseline_commit": normalized.baseline_commit,
                        "diff_hash": normalized.diff_hash,
                        "configuration_hash": normalized.configuration_hash,
                        "failures": list(normalized.failures),
                    },
                    "trace_hash": trace_hash,
                }
            )
    examples.sort(key=lambda item: item["example_id"])
    return examples, dict(sorted(exclusions.items()))


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    payload = "".join(f"{_canonical_json(record)}\n" for record in records)
    path.write_text(payload, encoding="utf-8")


def _manifest_without_hash(
    *,
    examples: list[dict[str, Any]],
    source_run_ids: list[str],
    source_schema_version: int,
    exclusions: dict[str, int],
    files: dict[str, str],
) -> dict[str, Any]:
    counts = Counter(example["split"] for example in examples)
    roles = Counter(example["role"] for example in examples)
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "trace_schema_version": DSPY_TRACE_SCHEMA_VERSION,
        "source_schema_version": source_schema_version,
        "dataset_hash": stable_hash(examples),
        "split_policy": "sha256(task) modulo 10: train=0-7, dev=8, holdout=9",
        "counts": {
            "total": len(examples),
            "by_split": {name: counts.get(name, 0) for name in SPLIT_NAMES},
            "by_role": {role: roles.get(role, 0) for role in ROLE_PROGRAMS},
        },
        "source_run_ids": sorted(source_run_ids),
        "exclusions": exclusions,
        "files": files,
    }


def export_gepa_dataset(
    database: Path,
    output: Path,
    *,
    run_ids: Iterable[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Export complete audit traces without mutating the source database."""
    if limit <= 0:
        raise GepaDatasetError("limit must be positive.")
    database = database.resolve()
    output = output.resolve()
    if output == database or output in database.parents:
        raise GepaDatasetError("Output directory cannot contain the source database.")
    try:
        store = StateStore(database, read_only=True)
    except FileNotFoundError as exc:
        raise GepaDatasetError(f"Run database does not exist: {database}") from exc
    source_schema_version = store.schema_version()
    selected_run_ids = list(dict.fromkeys(run_ids or ()))
    if not selected_run_ids:
        selected_run_ids = [row["id"] for row in store.recent_runs(limit)]
    records: list[dict[str, Any]] = []
    for run_id in selected_run_ids:
        details = store.run_details(run_id)
        if details is None:
            raise GepaDatasetError(f"Unknown run ID: {run_id}")
        records.append(details)
    examples, exclusions = build_gepa_examples(records)
    if not examples:
        raise GepaDatasetError("No eligible complete DSPy traces were found.")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "dataset"
        staging.mkdir()
        _write_jsonl(staging / "examples.jsonl", examples)
        for split in SPLIT_NAMES:
            _write_jsonl(
                staging / f"{split}.jsonl",
                (item for item in examples if item["split"] == split),
            )
        file_hashes = {
            path.name: _sha256_file(path) for path in sorted(staging.glob("*.jsonl"))
        }
        manifest = _manifest_without_hash(
            examples=examples,
            source_run_ids=selected_run_ids,
            source_schema_version=source_schema_version,
            exclusions=exclusions,
            files=file_hashes,
        )
        manifest["manifest_hash"] = stable_hash(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output.exists():
            if not output.is_dir():
                raise GepaDatasetError(f"Output path is not a directory: {output}")
            expected_names = {
                "manifest.json",
                "examples.jsonl",
                "train.jsonl",
                "dev.jsonl",
                "holdout.jsonl",
            }
            existing_names = {item.name for item in output.iterdir()}
            if (
                "manifest.json" not in existing_names
                or not existing_names <= expected_names
            ):
                raise GepaDatasetError(
                    "Refusing to replace a directory that is not a GEPA dataset."
                )
            shutil.rmtree(output)
        os.replace(staging, output)
    return manifest


def load_gepa_dataset(output: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and verify a previously exported dataset directory."""
    output = output.resolve()
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise GepaDatasetError("Dataset manifest is missing.")
    manifest = _json_object(
        manifest_path.read_text(encoding="utf-8"), label="dataset manifest"
    )
    claimed_manifest_hash = manifest.get("manifest_hash")
    hash_input = dict(manifest)
    hash_input.pop("manifest_hash", None)
    if claimed_manifest_hash != stable_hash(hash_input):
        raise GepaDatasetError("Dataset manifest hash does not match.")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise GepaDatasetError("Dataset manifest file table is malformed.")
    for name, claimed_hash in files.items():
        path = output / name
        if not isinstance(name, str) or not isinstance(claimed_hash, str):
            raise GepaDatasetError("Dataset manifest file entry is malformed.")
        if not path.is_file() or _sha256_file(path) != claimed_hash:
            raise GepaDatasetError(f"Dataset file hash does not match: {name}")
    examples: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        (output / "examples.jsonl").read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GepaDatasetError(
                f"Dataset example line {line_number} is malformed JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise GepaDatasetError("Dataset example is not a JSON object.")
        examples.append(decoded)
    if stable_hash(examples) != manifest.get("dataset_hash"):
        raise GepaDatasetError("Dataset content hash does not match.")
    return manifest, examples


def to_dspy_examples(
    records: Iterable[Mapping[str, Any]],
    *,
    dspy_module: Any | None = None,
) -> list[Any]:
    """Convert exported records into DSPy Examples without running an optimizer."""
    if dspy_module is None:
        try:
            import dspy as dspy_module
        except ImportError as exc:
            raise RuntimeError(
                "DSPy is not installed. Run `make agent-install`."
            ) from exc
    examples: list[Any] = []
    for record in records:
        inputs = dict(record["inputs"])
        inputs.pop("task", None)
        example = dspy_module.Example(
            task=record["task"],
            role=record["role"],
            evidence=_canonical_json(inputs),
            output=_canonical_json(record["output"]),
            verification_passed=record["outcome"]["verification_passed"],
            reviewer_verdict=record["outcome"]["reviewer_verdict"],
            reviewer_feedback=record["outcome"]["reviewer_feedback"],
            score=record["outcome"]["score"],
        ).with_inputs("task", "role", "evidence")
        examples.append(example)
    return examples
