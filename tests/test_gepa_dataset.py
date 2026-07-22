from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.dspy_programs.gepa_dataset import (
    GepaDatasetError,
    build_gepa_examples,
    export_gepa_dataset,
    load_gepa_dataset,
    to_dspy_examples,
)
from runtime.dspy_trace import record_dspy_trace
from runtime.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
CLI_SPEC = importlib.util.spec_from_file_location(
    "local_coder_gepa_cli", ROOT / "local-coder.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
local_coder = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(local_coder)


def _review(verdict: str = "pass") -> dict[str, object]:
    return {
        "verdict": verdict,
        "summary": "The audited change matches the task and deterministic evidence.",
        "issues": [],
        "unrelated_changes": [],
    }


def _complete_run(
    store: StateStore,
    *,
    task: str = "Change one canary value.",
    role: str = "planner",
    protected: bool = False,
) -> str:
    route = {
        "explorer": "local-plan",
        "planner": "local-plan",
        "implementer": "local-fast",
        "repairer": "local-fast",
        "reviewer": "local-review",
    }[role]
    program = {
        "explorer": "ExplorerProgram",
        "planner": "PlannerProgram",
        "implementer": "ImplementerProgram",
        "repairer": "RepairerProgram",
        "reviewer": "ReviewerProgram",
    }[role]
    run_id = store.create_run(
        task=task,
        mode="agentic",
        repository=store.path.parent,
        base_branch="main",
    )
    store.register_agent(
        run_id,
        role=role,
        skill=f"{role}-skill",
        model_route=route,
    )
    evidence = (
        "evaluation/holdout/secret.json" if protected else "1: LIVE_E2E_SENTINEL=before"
    )
    trace_inputs: dict[str, object] = {
        "task": f"# Agent Task\n\n{task}\n",
    }
    if role in {"explorer", "planner"}:
        trace_inputs.update(
            delegated_task="Inspect and propose one bounded result.",
            repository_evidence=[evidence],
        )
    elif role == "implementer":
        trace_inputs.update(
            instruction="Replace one exact value.",
            editable_files=["canary.txt"],
            file_contents=[f"--- canary.txt ---\n{evidence}"],
        )
    elif role == "repairer":
        trace_inputs.update(
            delegated_task="Repair one deterministic failure.",
            verification_output="make verify failed",
            diff="diff --git a/canary.txt b/canary.txt",
            editable_files=["canary.txt"],
            file_contents=[f"--- canary.txt ---\n{evidence}"],
        )
    else:
        trace_inputs.update(
            changed_files=["canary.txt"],
            verification_passed=True,
            verification_output="make verify passed",
            diff="diff --git a/canary.txt b/canary.txt",
        )
    trace_output: dict[str, object]
    if role == "explorer":
        trace_output = {
            "findings": ["The canary contains the before value."],
            "relevant_files": ["canary.txt"],
            "constraints": ["Change no other file."],
            "unresolved_questions": [],
        }
    elif role == "planner":
        trace_output = {
            "instruction": "Replace one exact canary value.",
            "editable_files": ["canary.txt"],
            "acceptance_criteria": ["make verify passes"],
            "depends_on": [],
        }
    elif role == "implementer":
        trace_output = {
            "edits": [
                {
                    "path": "canary.txt",
                    "old_text": "before",
                    "new_text": "after",
                }
            ]
        }
    elif role == "repairer":
        trace_output = {
            "diagnosis": "The canary retains the failing value.",
            "edits": [
                {
                    "path": "canary.txt",
                    "old_text": "before",
                    "new_text": "after",
                }
            ],
        }
    else:
        trace_output = _review()
    record_dspy_trace(
        store,
        run_id,
        role=role,
        program=program,
        route=route,
        inputs=trace_inputs,
        output=trace_output,
    )
    store.add_model_metrics(
        run_id,
        route=route,
        prompt_tokens=10,
        completion_tokens=4,
        metadata={
            "status": "success",
            "source": f"dspy-{role}",
            "program": program,
            "adapter": "JSONAdapter",
        },
    )
    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="138 passed",
        duration_ms=10.0,
    )
    store.add_artifact(
        run_id,
        kind="review",
        content=json.dumps(_review(), sort_keys=True),
    )
    store.update_run(run_id, status="awaiting_approval", result="complete")
    return run_id


def _directory_bytes(path: Path) -> dict[str, bytes]:
    return {
        item.name: item.read_bytes()
        for item in sorted(path.iterdir())
        if item.is_file()
    }


def test_export_is_read_only_and_byte_deterministic(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    store = StateStore(database)
    run_id = _complete_run(store)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = export_gepa_dataset(
        database,
        first,
        run_ids=[run_id],
    )
    second_manifest = export_gepa_dataset(
        database,
        second,
        run_ids=[run_id],
    )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert first_manifest == second_manifest
    assert _directory_bytes(first) == _directory_bytes(second)
    assert first_manifest["counts"]["total"] == 1
    assert first_manifest["counts"]["by_role"]["planner"] == 1


def test_examples_group_identical_tasks_into_one_split(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    first = _complete_run(store, task="Same task", role="explorer")
    second = _complete_run(store, task="Same task", role="planner")

    examples, exclusions = build_gepa_examples(
        [store.run_details(first), store.run_details(second)]
    )

    assert exclusions == {}
    assert {item["split"] for item in examples} == {examples[0]["split"]}
    assert {item["role"] for item in examples} == {"explorer", "planner"}


def test_export_excludes_protected_and_incomplete_records(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    valid = _complete_run(store, role="implementer")
    protected = _complete_run(store, role="planner", protected=True)
    incomplete = store.create_run(
        task="No trace",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )

    examples, exclusions = build_gepa_examples(
        [
            store.run_details(valid),
            store.run_details(protected),
            store.run_details(incomplete),
        ]
    )

    assert [item["run_id"] for item in examples] == [valid]
    assert exclusions["example references protected holdout or oracle material."] == 1
    assert exclusions["run has no deterministic verification result."] == 1


def test_export_requires_matching_backend_marker(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = _complete_run(store, role="reviewer")
    details = store.run_details(run_id)
    details["model_metrics"][0]["metadata"] = json.dumps(
        {
            "status": "success",
            "source": "dspy-reviewer",
            "program": "WrongProgram",
            "adapter": "JSONAdapter",
        }
    )

    examples, exclusions = build_gepa_examples([details])

    assert examples == []
    assert exclusions["DSPy trace lacks a matching successful backend audit."] == 1


def test_export_rejects_error_backend_marker(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = _complete_run(store, role="planner")
    details = store.run_details(run_id)
    metadata = json.loads(details["model_metrics"][0]["metadata"])
    metadata["status"] = "error"
    details["model_metrics"][0]["metadata"] = json.dumps(metadata)

    examples, exclusions = build_gepa_examples([details])

    assert examples == []
    assert exclusions["DSPy trace lacks a matching successful backend audit."] == 1


def test_load_wraps_malformed_jsonl(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    store = StateStore(database)
    _complete_run(store)
    output = tmp_path / "dataset"
    manifest = export_gepa_dataset(database, output)
    (output / "examples.jsonl").write_text("{not-json}\n", encoding="utf-8")
    bad_hash = hashlib.sha256((output / "examples.jsonl").read_bytes()).hexdigest()
    manifest["files"]["examples.jsonl"] = bad_hash
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("manifest_hash")
    from evaluation.outcomes import stable_hash

    manifest["manifest_hash"] = stable_hash(manifest_without_hash)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GepaDatasetError, match="malformed JSON"):
        load_gepa_dataset(output)


def test_export_rejects_role_payload_with_missing_fields(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = _complete_run(store, role="planner")
    details = store.run_details(run_id)
    trace = next(
        artifact
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
    )
    payload = json.loads(trace["content"])
    payload["output"].pop("acceptance_criteria")
    trace["content"] = json.dumps(payload)

    examples, exclusions = build_gepa_examples([details])

    assert examples == []
    assert exclusions["DSPy trace acceptance_criteria must be a list of strings."] == 1


def test_export_rejects_trace_from_another_task(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = _complete_run(store, task="Audited task", role="explorer")
    details = store.run_details(run_id)
    trace = next(
        artifact
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
    )
    payload = json.loads(trace["content"])
    payload["inputs"]["task"] = "# Agent Task\n\nDifferent task\n"
    trace["content"] = json.dumps(payload)

    examples, exclusions = build_gepa_examples([details])

    assert examples == []
    assert exclusions["DSPy trace task does not match its audited run."] == 1


def test_load_rejects_tampered_jsonl(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    store = StateStore(database)
    _complete_run(store)
    output = tmp_path / "dataset"
    export_gepa_dataset(database, output)
    with (output / "examples.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")

    with pytest.raises(GepaDatasetError, match="file hash"):
        load_gepa_dataset(output)


def test_dspy_conversion_marks_only_program_inputs(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = _complete_run(store)
    records, _ = build_gepa_examples([store.run_details(run_id)])

    class FakeExample(dict):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(kwargs)
            self.input_names: tuple[str, ...] = ()

        def with_inputs(self, *names: str) -> "FakeExample":
            self.input_names = names
            return self

    examples = to_dspy_examples(
        records,
        dspy_module=SimpleNamespace(Example=FakeExample),
    )

    assert len(examples) == 1
    assert examples[0].input_names == ("task", "role", "evidence")
    assert examples[0]["verification_passed"] is True
    assert examples[0]["reviewer_verdict"] == "pass"
    assert examples[0]["score"] == 1.0


def test_cli_exports_selected_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "agent.db"
    store = StateStore(database)
    run_id = _complete_run(store, role="explorer")
    output = tmp_path / "dataset"
    args = SimpleNamespace(
        database=database,
        output=output,
        limit=100,
        run_id=[run_id],
    )

    assert local_coder.handle_export_gepa_dataset(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"]["total"] == 1
    assert (output / "manifest.json").is_file()


def test_export_fails_closed_when_no_complete_traces(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    StateStore(database).create_run(
        task="Incomplete",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )

    with pytest.raises(GepaDatasetError, match="No eligible"):
        export_gepa_dataset(database, tmp_path / "dataset")


def test_export_refuses_to_replace_unrelated_directory(tmp_path: Path) -> None:
    database = tmp_path / "state" / "agent.db"
    store = StateStore(database)
    _complete_run(store)
    output = tmp_path / "unrelated"
    output.mkdir()
    (output / "keep.txt").write_text("do not delete", encoding="utf-8")

    with pytest.raises(GepaDatasetError, match="not a GEPA dataset"):
        export_gepa_dataset(database, output)

    assert (output / "keep.txt").read_text(encoding="utf-8") == "do not delete"


def test_export_refuses_output_ancestor_of_database(tmp_path: Path) -> None:
    database = tmp_path / "state" / "agent.db"
    store = StateStore(database)
    _complete_run(store)

    with pytest.raises(GepaDatasetError, match="contain the source database"):
        export_gepa_dataset(database, tmp_path / "state")
