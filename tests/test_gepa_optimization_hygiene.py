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
    BoundedNoImprovementStopper,
    BudgetedLM,
    CompactInstructionProposer,
    GepaRunnerError,
    ModelCallBudget,
    ModelCallBudgetExceeded,
    assess_candidate_instructions,
    assess_dataset_readiness,
    run_gepa_optimization,
)


class FakeExample:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)
        self._inputs: tuple[str, ...] = ()

    def with_inputs(self, *names: str) -> "FakeExample":
        self._inputs = names
        return self


class FakePrediction(dict):
    def __init__(self, **values: object) -> None:
        super().__init__(values)
        self.__dict__.update(values)


class FakeProgram:
    def __init__(
        self,
        outputs: dict[str, dict[str, object]],
        *,
        instructions: str,
        saved_payload: str,
    ) -> None:
        self.outputs = outputs
        self.instructions = instructions
        self.saved_payload = saved_payload
        self.lm = None

    def set_lm(self, lm: object) -> None:
        self.lm = lm

    def __call__(self, **inputs: object) -> FakePrediction:
        return FakePrediction(**self.outputs[str(inputs["task"])])

    def named_predictors(self) -> list[tuple[str, object]]:
        signature = SimpleNamespace(instructions=self.instructions)
        return [("predict.predict", SimpleNamespace(signature=signature))]

    def predictors(self) -> list[object]:
        return [object()]

    def save(self, path: str) -> None:
        Path(path).write_text(self.saved_payload, encoding="utf-8")


class FakeOptimized(FakeProgram):
    def __init__(
        self,
        outputs: dict[str, dict[str, object]],
        *,
        instructions: str,
        scores: list[float],
    ) -> None:
        super().__init__(
            outputs,
            instructions=instructions,
            saved_payload='{"optimized":true}\n',
        )
        self.detailed_results = SimpleNamespace(
            val_aggregate_scores=scores,
            total_metric_calls=17,
            num_full_val_evals=2,
            seed=0,
        )


class RaisingGEPA:
    def __init__(self, **_kwargs: object) -> None:
        raise AssertionError("GEPA should not be constructed")


class RecordingGEPA:
    calls: list[dict[str, object]] = []
    optimized: FakeOptimized

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
        return self.optimized


class FakeDSPy(SimpleNamespace):
    def __init__(self, gepa_type: type[object]) -> None:
        super().__init__(
            Example=FakeExample,
            Prediction=FakePrediction,
            JSONAdapter=lambda: object(),
            context=lambda **_kwargs: nullcontext(),
            GEPA=gepa_type,
        )


def _output(index: int) -> dict[str, object]:
    return {
        "instruction": f"Change canary {index}.",
        "editable_files": [f"canary-{index}.txt"],
        "acceptance_criteria": ["The exact value changes."],
        "depends_on": [],
    }


def _record(index: int, split: str, *, score: float = 1.0) -> dict[str, object]:
    task = f"Planner task {index}"
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
        "output": _output(index),
        "outcome": {
            "score": score,
            "reviewer_feedback": "Verdict: pass\nAudited outcome.",
            "reviewer_verdict": "pass",
            "verification_passed": True,
            "verification_output": "Verification: PASS",
        },
        "trace_hash": f"trace-{index}",
    }


def _write_dataset(path: Path, records: list[dict[str, object]]) -> None:
    path.mkdir()
    for filename, selected in (
        ("examples.jsonl", records),
        *(
            (
                f"{split}.jsonl",
                [record for record in records if record["split"] == split],
            )
            for split in ("train", "dev", "holdout")
        ),
    ):
        payload = "".join(
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
            for record in selected
        )
        (path / filename).write_text(payload, encoding="utf-8")
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


def _records(*, imperfect_train: bool) -> list[dict[str, object]]:
    return [
        _record(1, "train", score=0.5 if imperfect_train else 1.0),
        _record(2, "train"),
        _record(3, "dev"),
        _record(4, "holdout"),
    ]


def _exact_outputs(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["task"]): dict(record["output"]) for record in records}


def test_readiness_keeps_legacy_counts_and_reports_imperfect_train() -> None:
    readiness = assess_dataset_readiness(_records(imperfect_train=True), role="planner")

    assert readiness["ready"] is True
    assert readiness["imperfect_train"] == 1
    assert "imperfect_train" not in readiness["counts"]


def test_perfect_only_training_requires_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    records = _records(imperfect_train=False)
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, records)

    with pytest.raises(GepaRunnerError, match="no imperfect training examples"):
        run_gepa_optimization(
            dataset,
            tmp_path / "run",
            role="planner",
            dspy_module=FakeDSPy(RaisingGEPA),
        )


def test_perfect_baseline_skips_search_and_scores_holdout(tmp_path: Path) -> None:
    records = _records(imperfect_train=False)
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, records)
    student = FakeProgram(
        _exact_outputs(records),
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    lm_calls: list[tuple[str, dict[str, object]]] = []

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            output,
            role="planner",
            allow_perfect_only=True,
            dspy_module=FakeDSPy(RaisingGEPA),
            lm_factory=lambda route, **kwargs: (
                lm_calls.append((route, kwargs)) or f"lm:{route}"
            ),
        )

    optimization = result["report"]["optimization"]
    assert lm_calls == [("local-plan", {})]
    assert optimization["search_performed"] is False
    assert optimization["optimization_outcome"] == "baseline_perfect_no_search"
    assert optimization["winning_candidate"] == "baseline"
    assert result["report"]["holdout"]["exposed_during_optimization"] is False
    assert result["report"]["holdout"]["metric_calls"] == 1
    assert (output / "candidate.json").read_text(encoding="utf-8") == (
        '{"baseline":true}\n'
    )


def test_bounded_search_accepts_only_strict_safe_improvement(tmp_path: Path) -> None:
    records = _records(imperfect_train=True)
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, records)
    exact = _exact_outputs(records)
    baseline_outputs = {task: dict(value) for task, value in exact.items()}
    baseline_outputs["Planner task 3"] = {
        **baseline_outputs["Planner task 3"],
        "acceptance_criteria": ["Wrong development criterion."],
    }
    baseline_outputs["Planner task 4"] = {
        **baseline_outputs["Planner task 4"],
        "acceptance_criteria": ["Wrong holdout criterion."],
    }
    student = FakeProgram(
        baseline_outputs,
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    RecordingGEPA.calls = []
    RecordingGEPA.optimized = FakeOptimized(
        exact,
        instructions="Improved concise planner instruction.",
        scores=[0.75, 1.0],
    )
    lm_calls: list[tuple[str, dict[str, object]]] = []

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            output,
            role="planner",
            target_metric_calls=40,
            reflection_max_tokens=321,
            dspy_module=FakeDSPy(RecordingGEPA),
            lm_factory=lambda route, **kwargs: (
                lm_calls.append((route, kwargs)) or f"lm:{route}"
            ),
        )

    call = RecordingGEPA.calls[0]
    assert len(call["trainset"]) == 2
    assert len(call["valset"]) == 1
    assert call["kwargs"]["max_metric_calls"] == 40
    assert "stop_callbacks" in call["kwargs"]["gepa_kwargs"]
    assert lm_calls == [
        ("local-plan", {}),
        ("local-plan", {"max_tokens": 321}),
    ]
    optimization = result["report"]["optimization"]
    assert optimization["candidate_accepted"] is True
    assert optimization["winning_candidate"] == "optimized"
    assert optimization["optimization_outcome"] == "improved_candidate"
    assert result["report"]["holdout"]["delta"] > 0
    assert result["report"]["holdout"]["exposed_during_optimization"] is False
    assert (output / "candidate.json").read_text(encoding="utf-8") == (
        '{"optimized":true}\n'
    )


def test_unsafe_improvement_is_rejected(tmp_path: Path) -> None:
    records = _records(imperfect_train=True)
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, records)
    exact = _exact_outputs(records)
    baseline_outputs = {task: dict(value) for task, value in exact.items()}
    baseline_outputs["Planner task 3"] = {
        **baseline_outputs["Planner task 3"],
        "acceptance_criteria": ["Wrong criterion."],
    }
    student = FakeProgram(
        baseline_outputs,
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    RecordingGEPA.calls = []
    RecordingGEPA.optimized = FakeOptimized(
        exact,
        instructions="## Inputs\n### task\nReplay the training example.",
        scores=[0.75, 1.0],
    )

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            output,
            role="planner",
            target_metric_calls=40,
            dspy_module=FakeDSPy(RecordingGEPA),
            lm_factory=lambda route, **_kwargs: f"lm:{route}",
        )

    optimization = result["report"]["optimization"]
    assert optimization["candidate_safe"] is False
    assert optimization["candidate_accepted"] is False
    assert optimization["optimization_outcome"] == "rejected_unsafe_candidate"
    assert optimization["winning_candidate"] == "baseline"
    assert optimization["proposed_candidate_changed"] is True
    assert optimization["selected_candidate_changed"] is False
    assert optimization["candidate_changed"] is False
    assert (output / "candidate.json").read_text(encoding="utf-8") == (
        '{"baseline":true}\n'
    )


def test_auto_light_is_converted_to_concrete_budget(tmp_path: Path) -> None:
    records = _records(imperfect_train=True)
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, records)
    exact = _exact_outputs(records)
    student = FakeProgram(
        exact,
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    RecordingGEPA.calls = []
    RecordingGEPA.optimized = FakeOptimized(
        exact,
        instructions="Baseline planner instruction.",
        scores=[1.0],
    )

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            tmp_path / "run",
            role="planner",
            auto="light",
            force_search_perfect_baseline=True,
            dspy_module=FakeDSPy(RecordingGEPA),
            lm_factory=lambda route, **_kwargs: f"lm:{route}",
        )

    assert RecordingGEPA.calls[0]["kwargs"]["max_metric_calls"] == 384
    assert result["report"]["budget"]["effective_target_metric_calls"] == 384


def test_candidate_instruction_sanitation_and_stopper() -> None:
    assessment = assess_candidate_instructions(
        {"predict.predict": "Baseline."},
        {
            "predict.predict": (
                "## Inputs\n"
                "Repeated substantive instruction line.\n"
                "Repeated substantive instruction line.\n"
                "Repeated substantive instruction line."
            )
        },
        max_instruction_chars=1600,
    )
    assert assessment["changed"] is True
    assert assessment["safe"] is False
    assert len(assessment["reasons"]) == 2

    stopper = BoundedNoImprovementStopper(target_metric_calls=100, patience=2)
    state = SimpleNamespace(
        total_num_evals=1,
        i=1,
        program_full_scores_val_set=[0.8],
    )
    assert stopper(state) is False
    state.i = 2
    assert stopper(state) is False
    state.i = 3
    assert stopper(state) is True
    assert stopper.reason == "no_improvement"


def test_compact_proposer_uses_feedback_only_and_rejects_replay_dump() -> None:
    proposer = CompactInstructionProposer(
        FakeDSPy(RaisingGEPA),
        role="planner",
        max_instruction_chars=120,
        max_unsafe_proposals=2,
    )
    calls: list[dict[str, object]] = []

    def safe_predictor(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(
            improved_instruction=(
                "Produce one atomic plan grounded in evidence and preserve "
                "unrelated behavior."
            )
        )

    proposer._predictor = safe_predictor
    candidate = {"predict.predict": "Baseline planner instruction."}
    reflective = {
        "predict.predict": [
            {
                "Inputs": {"task": "SECRET TASK TEXT"},
                "Generated Outputs": {"instruction": "SECRET OUTPUT"},
                "Feedback": "Mismatched fields: acceptance_criteria.",
            }
        ]
    }

    proposed = proposer(candidate, reflective, ["predict.predict"])

    assert proposed["predict.predict"].startswith("Produce one atomic plan")
    assert calls[0]["feedback_summary"] == (
        "Example 1: Mismatched fields: acceptance_criteria."
    )
    assert "SECRET" not in str(calls[0])
    assert proposer.summary()["accepted_by_proposer"] == 1

    proposer._predictor = lambda **_kwargs: SimpleNamespace(improved_instruction="")
    assert proposer(candidate, reflective, ["predict.predict"]) == candidate
    assert proposer.summary()["last_rejection_reasons"] == [
        "predict.predict instruction is blank"
    ]

    proposer._predictor = lambda **_kwargs: SimpleNamespace(
        improved_instruction="## Inputs\n### task\nReplay one example."
    )
    assert proposer(candidate, reflective, ["predict.predict"]) == candidate
    summary = proposer.summary()
    assert summary["rejected_by_proposer"] == 2
    assert summary["stop_requested"] is True


def test_budgeted_lm_copies_share_a_hard_call_limit() -> None:
    class FakeLM:
        def __init__(self) -> None:
            self.calls = 0

        def copy(self, **_kwargs: object) -> "FakeLM":
            return self

        def __call__(self, *_args: object, **_kwargs: object) -> str:
            self.calls += 1
            return "ok"

    raw = FakeLM()
    budget = ModelCallBudget(2)
    student = BudgetedLM(raw, budget, "student")
    copied = student.copy(temperature=0)

    assert student() == "ok"
    assert copied() == "ok"
    with pytest.raises(ModelCallBudgetExceeded, match="2/2"):
        copied()
    assert raw.calls == 2
    assert budget.summary()["total"] == 2


def test_hard_model_budget_reduces_the_approximate_gepa_target(
    tmp_path: Path,
) -> None:
    records = _records(imperfect_train=True)
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, records)
    exact = _exact_outputs(records)
    student = FakeProgram(
        exact,
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    RecordingGEPA.calls = []
    RecordingGEPA.optimized = FakeOptimized(
        exact,
        instructions="Baseline planner instruction.",
        scores=[1.0],
    )

    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            tmp_path / "run",
            role="planner",
            target_metric_calls=40,
            hard_model_call_limit=20,
            force_search_perfect_baseline=True,
            dspy_module=FakeDSPy(RecordingGEPA),
            lm_factory=lambda route, **_kwargs: f"lm:{route}",
        )

    assert RecordingGEPA.calls[0]["kwargs"]["max_metric_calls"] == 8
    budget = result["report"]["budget"]
    assert budget["requested_target_metric_calls"] == 40
    assert budget["effective_target_metric_calls"] == 8
    assert budget["hard_model_call_limit"] == 20
    accounting = result["report"]["metric_call_accounting"]
    assert accounting["target_metric_calls"] == 8
    assert accounting["target_overrun"] == 9


def test_swallowed_hard_budget_failure_rejects_apparent_improvement(
    tmp_path: Path,
) -> None:
    records = _records(imperfect_train=True)
    dataset = tmp_path / "dataset"
    output = tmp_path / "run"
    _write_dataset(dataset, records)
    exact = _exact_outputs(records)
    baseline_outputs = {task: dict(value) for task, value in exact.items()}
    baseline_outputs["Planner task 3"] = {
        **baseline_outputs["Planner task 3"],
        "acceptance_criteria": ["Wrong development criterion."],
    }

    class BudgetUsingProgram(FakeProgram):
        def __call__(self, **inputs: object) -> FakePrediction:
            assert callable(self.lm)
            self.lm()
            return super().__call__(**inputs)

    class FakeLM:
        def copy(self, **_kwargs: object) -> "FakeLM":
            return self

        def __call__(self, *_args: object, **_kwargs: object) -> str:
            return "ok"

    class SwallowingGEPA:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def compile(
            self,
            student: BudgetUsingProgram,
            *,
            trainset: list[FakeExample],
            valset: list[FakeExample],
        ) -> FakeOptimized:
            del trainset
            inputs = {
                name: getattr(valset[0], name)
                for name in ("task", "delegated_task", "repository_evidence")
            }
            for _ in range(20):
                try:
                    student(**inputs)
                except ModelCallBudgetExceeded:
                    # DSPy's evaluator catches normal exceptions per example.
                    pass
            return FakeOptimized(
                exact,
                instructions="Improved concise planner instruction.",
                scores=[0.75, 1.0],
            )

    student = BudgetUsingProgram(
        baseline_outputs,
        instructions="Baseline planner instruction.",
        saved_payload='{"baseline":true}\n',
    )
    with patch(
        "runtime.dspy_programs.gepa_runner._role_program",
        return_value=student,
    ):
        result = run_gepa_optimization(
            dataset,
            output,
            role="planner",
            target_metric_calls=40,
            hard_model_call_limit=14,
            dspy_module=FakeDSPy(SwallowingGEPA),
            lm_factory=lambda _route, **_kwargs: FakeLM(),
        )

    optimization = result["report"]["optimization"]
    accounting = result["report"]["model_call_accounting"]
    assert accounting["total"] == 14
    assert accounting["blocked_calls"] > 0
    assert optimization["candidate_accepted"] is False
    assert optimization["winning_candidate"] == "baseline"
    assert optimization["optimization_outcome"] == "hard_model_call_limit"
    assert (output / "candidate.json").read_text(encoding="utf-8") == (
        '{"baseline":true}\n'
    )
