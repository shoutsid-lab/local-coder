"""Offline, non-promoting GEPA optimization runner for one DSPy role."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from evaluation.outcomes import stable_hash
from runtime.dspy_lm import DSPY_ROUTES, build_dspy_lm

from .gepa_dataset import GepaDatasetError, load_gepa_dataset

GEPA_RUN_SCHEMA_VERSION = 1
ROLE_INPUT_FIELDS = {
    "explorer": ("task", "delegated_task", "repository_evidence"),
    "planner": ("task", "delegated_task", "repository_evidence"),
    "implementer": ("task", "instruction", "editable_files", "file_contents"),
    "repairer": (
        "task",
        "delegated_task",
        "verification_output",
        "diff",
        "editable_files",
        "file_contents",
    ),
    "reviewer": (
        "task",
        "changed_files",
        "verification_passed",
        "verification_output",
        "diff",
    ),
}
ROLE_OUTPUT_FIELDS = {
    "explorer": (
        "findings",
        "relevant_files",
        "constraints",
        "unresolved_questions",
    ),
    "planner": (
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
    ),
    "implementer": ("edits",),
    "repairer": ("diagnosis", "edits"),
    "reviewer": ("verdict", "summary", "issues", "unrelated_changes"),
}
ROLE_ROUTES = {
    "explorer": "local-plan",
    "planner": "local-plan",
    "implementer": "local-fast",
    "repairer": "local-fast",
    "reviewer": "local-review",
}


class GepaRunnerError(ValueError):
    """Raised when an offline optimization run cannot proceed safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prediction_value(prediction: Any, field: str) -> Any:
    if isinstance(prediction, Mapping):
        return prediction.get(field)
    return getattr(prediction, field, None)


def _field_similarity(expected: Any, actual: Any) -> float:
    if expected == actual:
        return 1.0
    if type(expected) is not type(actual):
        return 0.0
    if isinstance(expected, str):
        return difflib.SequenceMatcher(None, expected, actual).ratio()
    if isinstance(expected, list):
        expected_text = _canonical_json(expected)
        actual_text = _canonical_json(actual)
        return difflib.SequenceMatcher(None, expected_text, actual_text).ratio()
    if isinstance(expected, dict):
        expected_text = _canonical_json(expected)
        actual_text = _canonical_json(actual)
        return difflib.SequenceMatcher(None, expected_text, actual_text).ratio()
    return 0.0


def build_gepa_metric(role: str, *, dspy_module: Any) -> Callable[..., Any]:
    """Build a deterministic replay metric with textual audit feedback."""
    if role not in ROLE_OUTPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    output_fields = ROLE_OUTPUT_FIELDS[role]

    def metric(
        gold: Any,
        pred: Any,
        trace: Any = None,
        pred_name: str | None = None,
        pred_trace: Any = None,
    ) -> Any:
        del trace, pred_name, pred_trace
        similarities: list[float] = []
        mismatches: list[str] = []
        for field in output_fields:
            expected = getattr(gold, f"expected_{field}")
            actual = _prediction_value(pred, field)
            similarity = _field_similarity(expected, actual)
            similarities.append(similarity)
            if similarity < 1.0:
                mismatches.append(field)
        structural_score = mean(similarities) if similarities else 0.0
        audit_score = float(getattr(gold, "audit_score", 0.0))
        score = structural_score * (0.5 + 0.5 * audit_score)
        mismatch_text = (
            ", ".join(mismatches) if mismatches else "none; output matched the audit"
        )
        feedback = (
            f"Audited replay for role {role}. Mismatched fields: {mismatch_text}. "
            f"Historical outcome score: {audit_score:.1f}. "
            f"Reviewer feedback: {getattr(gold, 'audit_feedback', '')}"
        )
        prediction_type = getattr(dspy_module, "Prediction", None)
        if prediction_type is None:
            return {"score": score, "feedback": feedback}
        return prediction_type(score=score, feedback=feedback)

    return metric


def to_role_dspy_examples(
    records: Iterable[Mapping[str, Any]],
    *,
    role: str,
    dspy_module: Any,
) -> list[Any]:
    """Convert one role's exported records to its exact DSPy signature shape."""
    if role not in ROLE_INPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    examples: list[Any] = []
    for record in records:
        if record.get("role") != role:
            continue
        payload = dict(record["inputs"])
        for field in ROLE_OUTPUT_FIELDS[role]:
            payload[f"expected_{field}"] = record["output"][field]
        payload.update(
            example_id=record["example_id"],
            audit_score=float(record["outcome"]["score"]),
            audit_feedback=str(record["outcome"]["reviewer_feedback"]),
        )
        example = dspy_module.Example(**payload).with_inputs(*ROLE_INPUT_FIELDS[role])
        examples.append(example)
    return examples


def assess_dataset_readiness(
    records: Iterable[Mapping[str, Any]],
    *,
    role: str,
) -> dict[str, Any]:
    """Return deterministic blockers and warnings for one role optimization."""
    if role not in ROLE_INPUT_FIELDS:
        raise GepaRunnerError(f"Unsupported GEPA role: {role}")
    selected = [record for record in records if record.get("role") == role]
    splits = Counter(str(record.get("split")) for record in selected)
    tasks = {str(record.get("task", "")).strip() for record in selected}
    tasks.discard("")
    successful = sum(
        float(record["outcome"].get("score", 0.0)) >= 1.0 for record in selected
    )
    imperfect = len(selected) - successful
    blockers: list[str] = []
    warnings: list[str] = []
    if splits["train"] < 2:
        blockers.append("role requires at least two training examples")
    if splits["dev"] < 1:
        blockers.append("role requires at least one development example")
    if len(tasks) < 3:
        blockers.append("role requires at least three distinct authoritative tasks")
    if imperfect == 0:
        warnings.append("dataset has no imperfect audited examples for reflection")
    if splits["holdout"] == 0:
        warnings.append("dataset has no offline holdout examples for later comparison")
    return {
        "role": role,
        "ready": not blockers,
        "counts": {
            "total": len(selected),
            "train": splits["train"],
            "dev": splits["dev"],
            "holdout": splits["holdout"],
            "distinct_tasks": len(tasks),
            "successful": successful,
            "imperfect": imperfect,
        },
        "blockers": blockers,
        "warnings": warnings,
    }


def _role_program(role: str) -> Any:
    if role == "explorer":
        from .explorer import ExplorerProgram

        return ExplorerProgram()
    if role == "planner":
        from .planner import PlannerProgram

        return PlannerProgram()
    if role == "implementer":
        from .implementer import ImplementerProgram

        return ImplementerProgram()
    if role == "repairer":
        from .repairer import RepairerProgram

        return RepairerProgram()
    if role == "reviewer":
        from .reviewer import ReviewerProgram

        return ReviewerProgram()
    raise GepaRunnerError(f"Unsupported GEPA role: {role}")


def _score_value(result: Any) -> float:
    if isinstance(result, Mapping):
        return float(result["score"])
    return float(getattr(result, "score"))


def _evaluate_baseline(
    program: Any,
    examples: list[Any],
    metric: Callable[..., Any],
    *,
    input_fields: tuple[str, ...],
) -> dict[str, Any]:
    scores: list[dict[str, Any]] = []
    for example in examples:
        inputs = {field: getattr(example, field) for field in input_fields}
        prediction = program(**inputs)
        score = _score_value(metric(example, prediction))
        scores.append(
            {
                "example_id": getattr(example, "example_id"),
                "score": score,
            }
        )
    return {
        "aggregate_score": mean(item["score"] for item in scores) if scores else 0.0,
        "examples": scores,
    }


def _detailed_result_summary(program: Any) -> dict[str, Any]:
    details = getattr(program, "detailed_results", None)
    if details is None:
        return {}
    scores = [float(value) for value in getattr(details, "val_aggregate_scores", [])]
    best_index = max(range(len(scores)), key=scores.__getitem__) if scores else None
    return {
        "val_aggregate_scores": scores,
        "best_index": best_index,
        "best_score": scores[best_index] if best_index is not None else None,
        "total_metric_calls": getattr(details, "total_metric_calls", None),
        "num_full_val_evals": getattr(details, "num_full_val_evals", None),
        "seed": getattr(details, "seed", None),
    }


def _prepare_output(output: Path) -> None:
    if output.exists():
        raise GepaRunnerError(f"GEPA output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)


def _write_run_directory(
    output: Path,
    *,
    report: dict[str, Any],
    candidate_writer: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    _prepare_output(output)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "run"
        staging.mkdir()
        (staging / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if candidate_writer is not None:
            candidate_writer(staging / "candidate.json")
        files = {
            path.name: _sha256_file(path)
            for path in sorted(staging.iterdir())
            if path.is_file()
        }
        manifest = {
            "schema_version": GEPA_RUN_SCHEMA_VERSION,
            "dataset_hash": report["dataset_hash"],
            "role": report["role"],
            "dry_run": report["dry_run"],
            "files": files,
        }
        manifest["manifest_hash"] = stable_hash(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
    return manifest


def run_gepa_optimization(
    dataset: Path,
    output: Path,
    *,
    role: str,
    dry_run: bool = False,
    reflection_route: str = "local-plan",
    auto: str = "light",
    seed: int = 0,
    num_threads: int = 1,
    dspy_module: Any | None = None,
    lm_factory: Callable[[str], Any] = build_dspy_lm,
) -> dict[str, Any]:
    """Validate or run one offline GEPA optimization without activation."""
    if reflection_route not in DSPY_ROUTES:
        raise GepaRunnerError(
            f"Unsupported reflection route: {reflection_route}. "
            f"Choose one of {sorted(DSPY_ROUTES)}."
        )
    if auto not in {"light", "medium", "heavy"}:
        raise GepaRunnerError("GEPA auto budget must be light, medium, or heavy.")
    if num_threads <= 0:
        raise GepaRunnerError("GEPA num_threads must be positive.")
    dataset = dataset.resolve()
    output = output.resolve()
    if output == dataset or dataset in output.parents:
        raise GepaRunnerError("GEPA output directory cannot be inside the dataset.")
    try:
        dataset_manifest, records = load_gepa_dataset(dataset)
    except GepaDatasetError as exc:
        raise GepaRunnerError(str(exc)) from exc
    readiness = assess_dataset_readiness(records, role=role)
    report: dict[str, Any] = {
        "schema_version": GEPA_RUN_SCHEMA_VERSION,
        "dataset": str(dataset.resolve()),
        "dataset_hash": dataset_manifest["dataset_hash"],
        "dataset_schema_version": dataset_manifest.get("schema_version"),
        "role": role,
        "student_route": ROLE_ROUTES.get(role),
        "reflection_route": reflection_route,
        "auto": auto,
        "seed": seed,
        "num_threads": num_threads,
        "dry_run": dry_run,
        "readiness": readiness,
        "baseline": None,
        "optimization": None,
        "activation": "not_performed",
        "promotion": "not_performed",
    }
    if dry_run:
        manifest = _write_run_directory(output, report=report)
        return {"manifest": manifest, "report": report}
    if not readiness["ready"]:
        joined = "; ".join(readiness["blockers"])
        raise GepaRunnerError(f"Dataset is not ready for role {role}: {joined}")
    if dspy_module is None:
        try:
            import dspy as dspy_module
        except ImportError as exc:
            raise GepaRunnerError(
                "DSPy is not installed. Run `make agent-install`."
            ) from exc
    train_records = [
        record
        for record in records
        if record.get("role") == role and record.get("split") == "train"
    ]
    dev_records = [
        record
        for record in records
        if record.get("role") == role and record.get("split") == "dev"
    ]
    trainset = to_role_dspy_examples(train_records, role=role, dspy_module=dspy_module)
    devset = to_role_dspy_examples(dev_records, role=role, dspy_module=dspy_module)
    metric = build_gepa_metric(role, dspy_module=dspy_module)
    student = _role_program(role)
    student_lm = lm_factory(ROLE_ROUTES[role])
    reflection_lm = lm_factory(reflection_route)
    set_lm = getattr(student, "set_lm", None)
    if callable(set_lm):
        set_lm(student_lm)
    adapter = dspy_module.JSONAdapter()
    with tempfile.TemporaryDirectory(prefix="local-coder-gepa-log-") as log_dir:
        with dspy_module.context(adapter=adapter, track_usage=True):
            baseline = _evaluate_baseline(
                student,
                devset,
                metric,
                input_fields=ROLE_INPUT_FIELDS[role],
            )
            optimizer = dspy_module.GEPA(
                metric=metric,
                reflection_lm=reflection_lm,
                auto=auto,
                num_threads=num_threads,
                track_stats=True,
                log_dir=log_dir,
                seed=seed,
            )
            optimized = optimizer.compile(student, trainset=trainset, valset=devset)
    report["baseline"] = baseline
    report["optimization"] = _detailed_result_summary(optimized)

    def write_candidate(path: Path) -> None:
        optimized.save(str(path))

    manifest = _write_run_directory(
        output, report=report, candidate_writer=write_candidate
    )
    return {"manifest": manifest, "report": report}
