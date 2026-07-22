from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.outcomes import stable_hash
from runtime.dspy_programs.gepa_runner import (
    GepaRunnerError,
    assess_dataset_readiness,
    build_gepa_metric,
    run_gepa_optimization,
    to_role_dspy_examples,
)


class FakeExample:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)
        self._inputs: tuple[str, ...] = ()

    def with_inputs(self, *names: str) -> "FakeExample":
        self._inputs = names
        return self

    def inputs(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self._inputs}


class FakePrediction(dict):
    def __init__(self, **values: object) -> None:
        super().__init__(values)
        self.__dict__.update(values)


class FakeProgram:
    def __init__(self, outputs: dict[str, dict[str, object]]) -> None:
        self.outputs = outputs
        self.lm = None

    def set_lm(self, lm: object) -> None:
        self.lm = lm

    def __call__(self, **inputs: object) -> FakePrediction:
        return FakePrediction(**self.outputs[str(inputs["task"])])


class FakeOptimized:
    def __init__(self) -> None:
        self.detailed_results = SimpleNamespace(
            val_aggregate_scores=[0.5, 0.9],
            total_metric_calls=7,
            num_full_val_evals=2,
            seed=3,
        )

    def save(self, path: str) -> None:
        Path(path).write_text('{"compiled":true}\n', encoding="utf-8")


class FakeGEPA:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def compile(
        self,
        student: object,
        *,
        trainset: list[object],
        valset: list[object],
    ) -> FakeOptimized:
        self.calls.append(
            {
                "student": student,
                "trainset": trainset,
                "valset": valset,
                "kwargs": self.kwargs,
            }
        )
        return FakeOptimized()


FAKE_DSPY = SimpleNamespace(
    Example=FakeExample,
    Prediction=FakePrediction,
    JSONAdapter=lambda: object(),
    context=lambda **_kwargs: nullcontext(),
    GEPA=FakeGEPA,
)


def _planner_record(index: int, split: str) -> dict[str, object]:
    task = f"Planner task {index}"
    output = {
        "instruction": f"Change canary {index}.",
        "editable_files": [f"canary-{index}.txt"],
        "acceptance_criteria": ["The exact value changes."],
        "depends_on": [],
    }
    return {
        "schema_version": 2,
        "example_id": f"example-{index}",
        "run_id": f"run-{index}",
        "role": "planner",
        "program": "PlannerProgram",
        "route": "local-plan",
        "split": split,
        "task": task,
        "inputs": {
            "task": task,
            "delegated_task": "Create one atomic plan.",
            "repository_evidence": [f"canary-{index}.txt: before"],
        },
        "output": output,
        "outcome": {
            "score": 1.0,
            "reviewer_feedback": "Verdict: pass\nThe plan matched the task.",
            "reviewer_verdict": "pass",
            "verification_passed": True,
            "verification_output": "Verification: PASS",
        },
        "trace_hash": f"trace-{index}",
    }


def _write_dataset(path: Path, records: list[dict[str, object]]) -> None:
    path.mkdir()
    all_payload = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )
    (path / "examples.jsonl").write_text(all_payload, encoding="utf-8")
    for split in ("train", "dev", "holdout"):
        payload = "".join(
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
            for record in records
            if record["split"] == split
        )
        (path / f"{split}.jsonl").write_text(payload, encoding="utf-8")
    files = {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.glob("*.jsonl"))
    }
    manifest = {
        "schema_version": 2,
        "trace_schema_version": 1,
        "source_schema_version": 8,
        "dataset_hash": stable_hash(records),
        "split_policy": "test",
        "counts": {},
        "source_run_ids": sorted(str(record["run_id"]) for record in records),
        "exclusions": {},
        "files": files,
    }
    manifest["manifest_hash"] = stable_hash(manifest)
    (path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_readiness_blocks_undersized_role_dataset() -> None:
    readiness = assess_dataset_readiness([_planner_record(1, "train")], role="planner")

    assert readiness["ready"] is False
    assert readiness["counts"]["train"] == 1
    assert "at least one development example" in " ".join(readiness["blockers"])


def test_dry_run_writes_immutable_report_without_importing_dspy(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, [_planner_record(1, "train")])

    result = run_gepa_optimization(
        dataset,
        output,
        role="planner",
        dry_run=True,
    )

    assert result["report"]["readiness"]["ready"] is False
    assert (output / "report.json").is_file()
    assert not (output / "candidate.json").exists()
    assert result["manifest"]["dry_run"] is True
    with pytest.raises(GepaRunnerError, match="already exists"):
        run_gepa_optimization(
            dataset,
            output,
            role="planner",
            dry_run=True,
        )


def test_actual_run_refuses_incomplete_splits_before_model_calls(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, [_planner_record(1, "train")])

    with pytest.raises(GepaRunnerError, match="not ready"):
        run_gepa_optimization(
            dataset,
            tmp_path / "run",
            role="planner",
            dry_run=False,
            dspy_module=FAKE_DSPY,
        )


def test_role_examples_and_metric_return_feedback() -> None:
    record = _planner_record(1, "train")
    examples = to_role_dspy_examples([record], role="planner", dspy_module=FAKE_DSPY)
    metric = build_gepa_metric("planner", dspy_module=FAKE_DSPY)
    prediction = FakePrediction(**record["output"])

    result = metric(examples[0], prediction)

    assert examples[0].inputs() == record["inputs"]
    assert result.score == 1.0
    assert "Reviewer feedback" in result.feedback


def test_actual_runner_saves_candidate_and_scores_without_activation(
    tmp_path: Path,
) -> None:
    records = [
        _planner_record(1, "train"),
        _planner_record(2, "train"),
        _planner_record(3, "dev"),
        _planner_record(4, "holdout"),
    ]
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, records)
    outputs = {str(record["task"]): dict(record["output"]) for record in records}
    program = FakeProgram(outputs)
    lm_calls: list[str] = []

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=program,
    ):
        result = run_gepa_optimization(
            dataset,
            output,
            role="planner",
            reflection_route="local-review",
            auto="light",
            seed=3,
            dspy_module=FAKE_DSPY,
            lm_factory=lambda route: lm_calls.append(route) or f"lm:{route}",
        )

    assert lm_calls == ["local-plan", "local-review"]
    assert program.lm == "lm:local-plan"
    assert result["report"]["baseline"]["aggregate_score"] == 1.0
    assert result["report"]["optimization"]["best_score"] == 0.9
    assert result["report"]["activation"] == "not_performed"
    assert result["report"]["promotion"] == "not_performed"
    assert (output / "candidate.json").read_text(encoding="utf-8") == (
        '{"compiled":true}\n'
    )
    assert "candidate.json" in result["manifest"]["files"]


def test_runner_refuses_output_inside_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, [_planner_record(1, "train")])

    with pytest.raises(GepaRunnerError, match="cannot be inside"):
        run_gepa_optimization(
            dataset,
            dataset / "optimizer-run",
            role="planner",
            dry_run=True,
        )
