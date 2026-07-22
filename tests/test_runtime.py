import inspect
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.editor import (
    AtomicEdit,
    EditorError,
    apply_edits,
    parse_edit_content,
    request_and_apply,
    request_edits,
)
from runtime.models import (
    AuditedModel,
    ModelBudgetExceeded,
    ModelRegistry,
    ModelUsageBudget,
)
from runtime.plans import (
    PlanError,
    load_task_plan,
    parse_task_plan,
    render_step_task,
)
from runtime.skills import discover_skills
from runtime.skills_loader import discover_skills as discover_skill_catalog
from runtime.state import StateStore
from runtime.tools import (
    ToolContext,
    Worktree,
    create_worktree,
    remove_worktree,
)

ROOT = Path(__file__).resolve().parents[1]
CLI_SPEC = importlib.util.spec_from_file_location(
    "local_coder_cli", ROOT / "local-coder.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
local_coder = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(local_coder)
REVIEW_SPEC = importlib.util.spec_from_file_location(
    "review_diff_cli", ROOT / "review-diff.py"
)
assert REVIEW_SPEC is not None and REVIEW_SPEC.loader is not None
review_diff_cli = importlib.util.module_from_spec(REVIEW_SPEC)
REVIEW_SPEC.loader.exec_module(review_diff_cli)
EDITOR_SPEC = importlib.util.spec_from_file_location(
    "run_editor_cli", ROOT / "run-editor.py"
)
assert EDITOR_SPEC is not None and EDITOR_SPEC.loader is not None
run_editor_cli = importlib.util.module_from_spec(EDITOR_SPEC)
EDITOR_SPEC.loader.exec_module(run_editor_cli)


def test_audited_models_share_candidate_usage_budget(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="bounded",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    response = SimpleNamespace(
        token_usage=SimpleNamespace(input_tokens=4, output_tokens=2)
    )
    underlying = SimpleNamespace(generate=lambda *_args, **_kwargs: response)
    budget = ModelUsageBudget(
        max_calls=1,
        max_prompt_tokens=4,
        max_completion_tokens=2,
    )
    first = AuditedModel(
        underlying,
        route="local-plan",
        state=store,
        run_id=run_id,
        usage_budget=budget,
    )
    second = AuditedModel(
        underlying,
        route="local-fast",
        state=store,
        run_id=run_id,
        usage_budget=budget,
    )

    assert first.generate([]) is response
    with pytest.raises(ModelBudgetExceeded, match="model-call"):
        second.generate([])

    assert budget.calls == 1
    assert len(store.run_details(run_id)["model_metrics"]) == 1


def test_candidate_token_excess_is_recorded_before_run_stops(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="bounded",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    response = SimpleNamespace(
        token_usage=SimpleNamespace(input_tokens=5, output_tokens=1)
    )
    model = AuditedModel(
        SimpleNamespace(generate=lambda *_args, **_kwargs: response),
        route="local-plan",
        state=store,
        run_id=run_id,
        usage_budget=ModelUsageBudget(
            max_calls=2,
            max_prompt_tokens=4,
            max_completion_tokens=2,
        ),
    )

    with pytest.raises(ModelBudgetExceeded, match="prompt-token"):
        model.generate([])

    metric = store.run_details(run_id)["model_metrics"][0]
    assert metric["prompt_tokens"] == 5


def test_holdout_rotation_is_external_immutable_and_validated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manifest = source / "manifest.json"
    oracle = source / "oracle.json"
    manifest.write_text(
        '{"schema_version":1,"suite_id":"secret-v2",'
        '"visibility":"holdout","cases":['
        '{"id":"secret-case","scenario":"sequential_edits"}]}',
        encoding="utf-8",
    )
    oracle.write_text(
        '{"schema_version":1,"suite_id":"secret-v2",'
        '"cases":{"secret-case":{"content":"secret"}}}',
        encoding="utf-8",
    )
    storage = tmp_path / "trusted-storage"
    arguments = SimpleNamespace(
        rotation_id="rotation-2",
        manifest=manifest,
        oracle=oracle,
    )

    with patch.object(local_coder, "HOLDOUT_STORAGE", storage):
        assert local_coder.handle_rotate_holdout(arguments) == 0
        assert local_coder.handle_rotate_holdout(arguments) == 1

    destination = storage / "rotation-2"
    assert (destination / "manifest.json").read_bytes() == manifest.read_bytes()
    assert (destination / "oracle.json").read_bytes() == oracle.read_bytes()
    assert (destination / "oracle.json").stat().st_mode & 0o777 == 0o600


def test_tracked_repository_paths_are_not_secret_holdout_storage() -> None:
    assert (
        local_coder._is_external_holdout(
            ROOT / "evaluation" / "suites" / "atomic-v1.json"
        )
        is False
    )
    assert (
        local_coder._is_external_holdout(
            ROOT / ".local-coder" / "holdout" / "rotation" / "manifest.json"
        )
        is True
    )


def test_direct_cli_reenters_symlinked_project_virtualenv(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    base_python = tmp_path / "python"
    base_python.touch()
    venv_python = bin_dir / "python"
    venv_python.symlink_to(base_python)

    with (
        patch.object(local_coder, "VENV_PYTHON", venv_python),
        patch.object(local_coder.sys, "prefix", str(tmp_path / "base")),
        patch.object(local_coder.sys, "executable", str(base_python)),
        patch.dict(
            local_coder.os.environ,
            {"LOCAL_CODER_VENV_BOOTSTRAPPED": ""},
        ),
        patch.object(local_coder.os, "execve") as execve,
    ):
        local_coder.ensure_project_python()

    execve.assert_called_once()


def test_direct_editor_uses_instruction_when_task_file_is_omitted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = SimpleNamespace(
        instruction="Replace one exact README sentence.",
        files=["README.md"],
        task=None,
    )
    with (
        patch.object(run_editor_cli, "parse_args", return_value=arguments),
        patch.object(
            run_editor_cli,
            "request_and_apply",
            return_value=["README.md"],
        ) as request_and_apply,
    ):
        assert run_editor_cli.main() == 0

    assert capsys.readouterr().out == "Changed files: README.md\n"
    assert request_and_apply.call_args.kwargs["task"] == (
        "# Atomic Task\n\nReplace one exact README sentence.\n"
    )
    assert request_and_apply.call_args.kwargs["protected_files"] == set()


def test_cli_review_uses_project_python() -> None:
    arguments = SimpleNamespace(task=Path("task.md"))
    with patch.object(local_coder, "run_command", return_value=0) as run_command:
        assert local_coder.handle_review(arguments) == 0

    assert run_command.call_args.args[0] == [
        local_coder.sys.executable,
        "./review-diff.py",
        "--task",
        "task.md",
    ]


def test_reviewer_accepts_fenced_valid_json() -> None:
    content = """Result:\n```json\n{
      "verdict": "pass",
      "summary": "The atomic change satisfies the task.",
      "issues": [],
      "unrelated_changes": []
    }\n```"""

    assert review_diff_cli.parse_review_content(content)["verdict"] == "pass"


def test_reviewer_rejects_invalid_json_shape() -> None:
    with pytest.raises(review_diff_cli.ReviewError):
        review_diff_cli.parse_review_content('{"result": "pass"}')


def test_reviewer_normalizes_verdict_only_response() -> None:
    evidence = review_diff_cli.verdict_only_context(["README.md"], True)
    review = review_diff_cli.parse_review_content(
        '{"verdict": "pass"}',
        verdict_only_evidence=evidence,
    )

    assert review["verdict"] == "pass"
    assert "Deterministic verification passed for README.md" in review["summary"]
    assert "model supplied no additional explanation" in review["summary"]
    assert review["issues"] == []
    assert review["unrelated_changes"] == []


def test_reviewer_rejects_unknown_verdict() -> None:
    with pytest.raises(review_diff_cli.ReviewError):
        review_diff_cli.parse_review_content('{"verdict": "approve"}')


def test_reviewer_rejects_invalid_explanation_fields() -> None:
    content = """{
      "verdict": "fail",
      "summary": "A concrete issue exists.",
      "issues": [""],
      "unrelated_changes": []
    }"""

    with pytest.raises(review_diff_cli.ReviewError):
        review_diff_cli.parse_review_content(content)


def test_dspy_reviewer_preserves_verdict_contract_and_metrics() -> None:
    captured: dict[str, object] = {}
    metrics: list[dict[str, object]] = []

    class Prediction(SimpleNamespace):
        def get_lm_usage(self) -> dict[str, dict[str, int]]:
            return {
                "openai/local-review": {
                    "prompt_tokens": 120,
                    "completion_tokens": 24,
                }
            }

    def runner(**kwargs: object) -> Prediction:
        captured.update(kwargs)
        return Prediction(
            verdict="pass",
            summary="README.md matches the task and verification passed.",
            issues=[],
            unrelated_changes=[],
        )

    review = review_diff_cli.call_reviewer(
        model="local-review",
        task="Update README.md",
        changed_files=["README.md"],
        diff="diff --git a/README.md b/README.md",
        verification_passed=True,
        verification_output="Verification: PASS",
        metrics_callback=lambda **values: metrics.append(values),
        reviewer_runner=runner,
    )

    assert review == {
        "verdict": "pass",
        "summary": "README.md matches the task and verification passed.",
        "issues": [],
        "unrelated_changes": [],
    }
    assert captured["model"] == "local-review"
    assert captured["changed_files"] == ["README.md"]
    assert metrics[0]["prompt_tokens"] == 120
    assert metrics[0]["completion_tokens"] == 24
    assert metrics[0]["metadata"] == {
        "status": "success",
        "source": "dspy-reviewer",
        "program": "ReviewerProgram",
        "adapter": "JSONAdapter",
    }


def test_dspy_reviewer_rejects_invalid_typed_prediction() -> None:
    def runner(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            verdict="approve",
            summary="Not a valid verdict.",
            issues=[],
            unrelated_changes=[],
        )

    with pytest.raises(
        review_diff_cli.ReviewError,
        match="invalid structured response",
    ):
        review_diff_cli.call_reviewer(
            model="local-review",
            task="Update README.md",
            changed_files=["README.md"],
            diff="diff",
            verification_passed=True,
            verification_output="Verification: PASS",
            reviewer_runner=runner,
        )


def test_required_skills_load() -> None:
    skills = discover_skills(ROOT / ".local-coder" / "skills")

    assert set(skills) == {
        "explore-repository",
        "plan-change",
        "atomic-implementation",
        "test-and-repair",
        "review-change",
    }
    assert skills["atomic-implementation"].model == "local-fast"
    assert "apply_atomic_edit" in skills["atomic-implementation"].tools
    assert "read-only adapter" in skills["explore-repository"].instructions
    assert "do not emit code" in skills["explore-repository"].instructions
    assert skills["explore-repository"].max_steps == 1
    assert skills["plan-change"].max_steps == 1


def test_native_editor_parses_strict_edit_json() -> None:
    edits = parse_edit_content(
        '{"edits":[{"path":"README.md","old_text":"before","new_text":"after"}]}'
    )

    assert edits == [AtomicEdit("README.md", "before", "after")]


def test_native_editor_accepts_one_exact_json_fence() -> None:
    edits = parse_edit_content("""```json
{"edits":[{"path":"README.md","old_text":"before","new_text":"after"}]}
```""")

    assert edits == [AtomicEdit("README.md", "before", "after")]


def test_native_editor_rejects_prose_around_json() -> None:
    with pytest.raises(EditorError, match="invalid JSON"):
        parse_edit_content(
            'Result: {"edits":[{"path":"README.md","old_text":"a","new_text":"b"}]}'
        )


def test_native_editor_sends_strict_nested_json_schema() -> None:
    response = io.BytesIO(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "edits": [
                                        {
                                            "path": "README.md",
                                            "old_text": "before",
                                            "new_text": "after",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ).encode("utf-8")
    )
    metrics: list[dict[str, object]] = []

    with patch(
        "runtime.editor.urllib.request.urlopen", return_value=response
    ) as open_url:
        edits = request_edits(
            instruction="Replace one exact word",
            contents={"README.md": "before\n"},
            task="Change before to after",
            metrics_callback=lambda **values: metrics.append(values),
        )

    request = open_url.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert set(response_format) == {"type", "json_schema"}
    assert response_format["json_schema"]["name"] == "atomic_edits"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["edits"],
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "old_text", "new_text"],
                    "properties": {
                        "path": {"type": "string", "enum": ["README.md"]},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                    },
                },
            }
        },
    }
    prompt = payload["messages"][1]["content"]
    assert "Return exactly one JSON object with this shape" in prompt
    assert "The top-level object must contain only `edits`" in prompt
    assert edits == [AtomicEdit("README.md", "before", "after")]
    assert metrics[0]["metadata"] == {
        "status": "success",
        "source": "native-editor",
    }


def test_native_editor_audits_malformed_response_excerpt() -> None:
    malformed = '{"edits": [], "comment": "not allowed"}'
    response = io.BytesIO(
        json.dumps(
            {
                "choices": [{"message": {"content": malformed}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ).encode("utf-8")
    )
    metrics: list[dict[str, object]] = []

    with (
        patch("runtime.editor.urllib.request.urlopen", return_value=response),
        pytest.raises(EditorError, match="contain only `edits`"),
    ):
        request_edits(
            instruction="Replace one exact word",
            contents={"README.md": "before\n"},
            task="Change before to after",
            metrics_callback=lambda **values: metrics.append(values),
        )

    assert metrics[0]["metadata"] == {
        "status": "error",
        "source": "native-editor",
        "response_excerpt": malformed,
    }


def test_native_editor_applies_only_exact_unique_matches(tmp_path: Path) -> None:
    editable = tmp_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")

    changed = apply_edits(
        tmp_path,
        ["README.md"],
        [AtomicEdit("README.md", "before", "after")],
    )

    assert changed == ["README.md"]
    assert editable.read_text(encoding="utf-8") == "after\n"


def test_native_editor_fails_before_writing_ambiguous_edits(tmp_path: Path) -> None:
    editable = tmp_path / "README.md"
    editable.write_text("repeat repeat\n", encoding="utf-8")

    with pytest.raises(EditorError, match="match exactly once"):
        apply_edits(
            tmp_path,
            ["README.md"],
            [AtomicEdit("README.md", "repeat", "changed")],
        )

    assert editable.read_text(encoding="utf-8") == "repeat repeat\n"


def test_native_editor_validates_complete_batch_before_writing(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    first.write_text("first before\n", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text("second before\n", encoding="utf-8")
    generated = [
        AtomicEdit("first.txt", "first before", "first after"),
        AtomicEdit("second.txt", "missing text", "second after"),
    ]

    with (
        patch("runtime.editor.request_edits", return_value=generated),
        pytest.raises(EditorError, match="found 0"),
    ):
        request_and_apply(
            root=tmp_path,
            instruction="Update both approved files",
            editable_files=["first.txt", "second.txt"],
            task="Make two exact replacements",
        )

    assert first.read_text(encoding="utf-8") == "first before\n"
    assert second.read_text(encoding="utf-8") == "second before\n"


def test_native_editor_rejects_protected_files(tmp_path: Path) -> None:
    protected = tmp_path / "example_contract.py"
    protected.write_text("before\n", encoding="utf-8")

    with pytest.raises(EditorError, match="Protected file"):
        apply_edits(
            tmp_path,
            ["example_contract.py"],
            [AtomicEdit("example_contract.py", "before", "after")],
        )


def test_native_editor_rejects_normalized_protected_aliases(tmp_path: Path) -> None:
    protected = tmp_path / "Makefile"
    protected.write_text("before\n", encoding="utf-8")

    with pytest.raises(EditorError, match="Protected file"):
        apply_edits(
            tmp_path,
            ["./Makefile"],
            [AtomicEdit("./Makefile", "before", "after")],
        )


def _valid_task_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_id": "parser-cleanup-v1",
        "objective": "Improve parser error reporting in two reviewed steps.",
        "steps": [
            {
                "id": "implementation",
                "instruction": (
                    "Clarify the parser error message without changing behavior."
                ),
                "editable_files": ["runtime/parser.py"],
                "acceptance_criteria": [
                    "The error identifies the invalid token.",
                    "Existing parser behavior remains unchanged.",
                ],
                "depends_on": [],
            },
            {
                "id": "tests",
                "instruction": "Add a regression test for the clarified error message.",
                "editable_files": ["tests/test_parser.py"],
                "acceptance_criteria": [
                    "The regression test passes deterministically."
                ],
                "depends_on": ["implementation"],
            },
        ],
    }


def _write_plan_repository(tmp_path: Path) -> Path:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "runtime" / "parser.py").write_text(
        "def parse(value):\n    return value\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_parser.py").write_text(
        "def test_parser():\n    assert True\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "task-plan.json"
    plan_path.write_text(json.dumps(_valid_task_plan()), encoding="utf-8")
    return plan_path


def test_task_plan_is_strict_ordered_and_deterministically_hashed(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan_repository(tmp_path)

    first = load_task_plan(plan_path, repository=tmp_path)
    second = parse_task_plan(
        json.loads(plan_path.read_text(encoding="utf-8")),
        repository=tmp_path,
        plan_path=plan_path,
    )

    assert first.plan_hash == second.plan_hash
    assert first.step("tests").depends_on == ("implementation",)
    assert first.step("implementation").editable_files == ("runtime/parser.py",)
    assert len(first.plan_hash) == 64


def test_task_plan_rejects_later_dependencies_and_protected_plan_file(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan_repository(tmp_path)
    payload = _valid_task_plan()
    payload["steps"][0]["depends_on"] = ["tests"]

    with pytest.raises(PlanError, match="unknown or later steps"):
        parse_task_plan(payload, repository=tmp_path, plan_path=plan_path)

    payload = _valid_task_plan()
    payload["steps"][0]["editable_files"] = ["task-plan.json"]
    with pytest.raises(PlanError, match="Protected file"):
        parse_task_plan(payload, repository=tmp_path, plan_path=plan_path)

    payload = _valid_task_plan()
    payload["schema_version"] = True
    with pytest.raises(PlanError, match="schema version"):
        parse_task_plan(payload, repository=tmp_path, plan_path=plan_path)

    payload = _valid_task_plan()
    payload["steps"][0]["editable_files"] = [
        "runtime/parser.py",
        "./runtime/parser.py",
    ]
    with pytest.raises(PlanError, match="duplicate path aliases"):
        parse_task_plan(payload, repository=tmp_path, plan_path=plan_path)


def test_plan_step_task_freezes_hash_scope_and_acceptance_criteria(
    tmp_path: Path,
) -> None:
    plan = load_task_plan(_write_plan_repository(tmp_path), repository=tmp_path)

    task = render_step_task(plan, plan.step("implementation"))

    assert f"Plan hash: {plan.plan_hash}" in task
    assert "- runtime/parser.py" in task
    assert "The error identifies the invalid token." in task
    assert "Modify only the editable files listed above." in task


def test_run_plan_step_requires_hash_and_declared_dependencies(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan_repository(tmp_path)
    plan = load_task_plan(plan_path, repository=tmp_path)
    summary = SimpleNamespace(
        status="awaiting_approval",
        to_json=lambda: '{"status": "awaiting_approval"}',
    )
    arguments = SimpleNamespace(
        plan=plan_path,
        step_id="tests",
        approve_plan_hash=plan.plan_hash,
        completed_step=["implementation"],
        max_steps=8,
    )

    with (
        patch.object(local_coder, "ROOT", tmp_path),
        patch("runtime.orchestrator.AgentOrchestrator") as orchestrator,
    ):
        orchestrator.return_value.run.return_value = summary
        assert local_coder.handle_run_plan_step(arguments) == 0

    config = orchestrator.call_args.args[0]
    assert config.expected_changed_paths == ("tests/test_parser.py",)
    task = orchestrator.return_value.run.call_args.args[0]
    assert "Step ID: tests" in task
    assert f"Plan hash: {plan.plan_hash}" in task


def test_run_plan_step_rejects_unapproved_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = _write_plan_repository(tmp_path)
    arguments = SimpleNamespace(
        plan=plan_path,
        step_id="implementation",
        approve_plan_hash="0" * 64,
        completed_step=None,
        max_steps=8,
    )

    with patch.object(local_coder, "ROOT", tmp_path):
        assert local_coder.handle_run_plan_step(arguments) == 1

    assert "Approved plan hash does not match" in capsys.readouterr().err


def test_managed_agents_accept_positional_additional_arguments(tmp_path: Path) -> None:
    from runtime.agents import (
        DSPyImplementerAgent,
        DSPyRepairerAgent,
        ReadOnlyEvidenceAgent,
        ReadOnlyReviewAgent,
        build_agent_bundle,
    )

    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Inspect the repository",
        mode="agentic",
        repository=ROOT,
        base_branch="main",
    )
    task_file = tmp_path / "TASK.md"
    task_file.write_text("# Task\n\nInspect README.md.\n", encoding="utf-8")
    context = ToolContext(
        root=ROOT,
        worktree=Worktree(ROOT, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )
    catalog = discover_skill_catalog(ROOT / ".local-coder" / "skills")
    activation_patcher = patch.object(catalog, "activate", wraps=catalog.activate)
    activate = activation_patcher.start()
    bundle = build_agent_bundle(
        skills=catalog,
        context=context,
        models=ModelRegistry(),
        state=store,
        run_id=run_id,
    )
    assert activate.call_count == 0

    parameters = inspect.signature(bundle.managed[0].__call__).parameters

    assert all(isinstance(agent, ReadOnlyEvidenceAgent) for agent in bundle.managed[:2])
    assert isinstance(bundle.managed[2], DSPyImplementerAgent)
    assert isinstance(bundle.managed[3], DSPyRepairerAgent)
    assert isinstance(bundle.managed[4], ReadOnlyReviewAgent)
    assert "additional_args" in parameters
    assert bundle.manager.python_executor.timeout_seconds == 600
    assert bundle.manager.planning_interval is None

    explorer = bundle.managed[0]

    def explorer_usage() -> dict[str, dict[str, int]]:
        return {
            "openai/local-plan": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
            }
        }

    explorer_prediction = SimpleNamespace(
        findings=["README.md defines the project overview."],
        relevant_files=["README.md"],
        constraints=["Keep the existing architecture links intact."],
        unresolved_questions=[],
        get_lm_usage=explorer_usage,
    )
    with patch.object(
        explorer,
        "program_runner",
        return_value=explorer_prediction,
    ) as run_program:
        assert explorer("Inspect README.md", {"request": "context"}) == (
            "Read-only findings:\n"
            "- README.md defines the project overview.\n\n"
            "Relevant files:\n- README.md\n\n"
            "Constraints:\n- Keep the existing architecture links intact."
        )
    assert activate.call_args.args == ("explore-repository",)
    assert "# local-coder" in run_program.call_args.kwargs["repository_evidence"][0]
    assert run_program.call_args.kwargs["delegated_task"] == "Inspect README.md"
    details = store.run_details(run_id)
    assert details is not None
    assert details["tool_calls"][0]["agent_role"] == "explorer"
    assert details["tool_calls"][0]["tool_name"] == "read_file"
    explorer_metric = details["model_metrics"][-1]
    assert explorer_metric["route"] == "local-plan"
    assert explorer_metric["prompt_tokens"] == 11
    assert explorer_metric["completion_tokens"] == 7
    assert json.loads(explorer_metric["metadata"]) == {
        "source": "dspy-explorer",
        "program": "ExplorerProgram",
        "adapter": "JSONAdapter",
        "status": "success",
    }
    explorer_traces = [
        json.loads(artifact["content"])
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
        and json.loads(artifact["content"])["role"] == "explorer"
    ]
    assert explorer_traces[-1]["output"]["relevant_files"] == ["README.md"]

    planner = bundle.managed[1]

    def planner_usage() -> dict[str, dict[str, int]]:
        return {
            "openai/local-plan": {
                "prompt_tokens": 13,
                "completion_tokens": 9,
            }
        }

    planner_prediction = SimpleNamespace(
        instruction=(
            "Replace the exact existing sentence in README.md with the requested "
            "sentence."
        ),
        editable_files=["README.md"],
        acceptance_criteria=[
            "README.md contains the requested sentence exactly once.",
            "make verify passes and the diff contains no unrelated changes.",
        ],
        depends_on=[],
        get_lm_usage=planner_usage,
    )
    with patch.object(
        planner,
        "program_runner",
        return_value=planner_prediction,
    ) as run_planner:
        planner_result = planner("Plan the README.md replacement")
    assert planner_result == (
        "Atomic instruction: Replace the exact existing sentence in README.md "
        "with the requested sentence.\n\n"
        "Editable files:\n- README.md\n\n"
        "Acceptance criteria:\n"
        "- README.md contains the requested sentence exactly once.\n"
        "- make verify passes and the diff contains no unrelated changes.\n\n"
        "Depends on: none"
    )
    assert run_planner.call_args.kwargs["delegated_task"] == (
        "Plan the README.md replacement"
    )
    details = store.run_details(run_id)
    assert details is not None
    planner_metric = details["model_metrics"][-1]
    assert json.loads(planner_metric["metadata"]) == {
        "source": "dspy-planner",
        "program": "PlannerProgram",
        "adapter": "JSONAdapter",
        "status": "success",
    }
    planner_traces = [
        json.loads(artifact["content"])
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
        and json.loads(artifact["content"])["role"] == "planner"
    ]
    assert planner_traces[-1]["output"]["editable_files"] == ["README.md"]

    implementer = bundle.managed[2]

    def implementer_usage() -> dict[str, dict[str, int]]:
        return {
            "openai/local-fast": {
                "prompt_tokens": 17,
                "completion_tokens": 12,
            }
        }

    implementer_prediction = SimpleNamespace(
        edits=[
            SimpleNamespace(
                path="README.md",
                old_text="# local-coder",
                new_text="# local-coder updated",
                model_dump=lambda: {
                    "path": "README.md",
                    "old_text": "# local-coder",
                    "new_text": "# local-coder updated",
                },
            )
        ],
        get_lm_usage=implementer_usage,
    )
    with (
        patch.object(
            implementer,
            "program_runner",
            return_value=implementer_prediction,
        ) as run_implementer,
        patch.object(
            implementer.context,
            "apply_prepared_atomic_edits",
            return_value="Atomic edit applied to: README.md",
        ) as apply_prepared,
    ):
        implementation_result = implementer("Replace one sentence in README.md")
    assert implementation_result == (
        "Implementation succeeded: Atomic edit applied to: README.md"
    )
    assert run_implementer.call_args.kwargs["editable_files"] == ["README.md"]
    assert "# local-coder" in run_implementer.call_args.kwargs["file_contents"][0]
    assert apply_prepared.call_args.args[:2] == (
        "Replace one sentence in README.md",
        "README.md",
    )
    details = store.run_details(run_id)
    assert details is not None
    implementer_metric = details["model_metrics"][-1]
    assert implementer_metric["route"] == "local-fast"
    assert implementer_metric["prompt_tokens"] == 17
    assert implementer_metric["completion_tokens"] == 12
    assert json.loads(implementer_metric["metadata"]) == {
        "source": "dspy-implementer",
        "program": "ImplementerProgram",
        "adapter": "JSONAdapter",
        "status": "success",
    }
    implementer_traces = [
        json.loads(artifact["content"])
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
        and json.loads(artifact["content"])["role"] == "implementer"
    ]
    assert implementer_traces[-1]["output"]["edits"][0]["path"] == "README.md"
    assert [call.args for call in activate.call_args_list] == [
        ("explore-repository",),
        ("plan-change",),
        ("atomic-implementation",),
    ]
    activation_patcher.stop()


def test_failed_skill_activation_is_audited_as_failed(tmp_path: Path) -> None:
    from runtime.agents import ReadOnlyEvidenceAgent

    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Inspect the repository",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )

    def fail_activation() -> None:
        raise ValueError("invalid skill")

    agent = ReadOnlyEvidenceAgent(
        name="explorer",
        description="Read-only evidence",
        activate_skill=fail_activation,
        context=SimpleNamespace(),
        model_route="local-plan",
        lm_factory=lambda: SimpleNamespace(),
        program_runner=lambda **_: SimpleNamespace(),
        program_name="ExplorerProgram",
        state=store,
        run_id=run_id,
    )

    with pytest.raises(ValueError, match="invalid skill"):
        agent("Inspect README.md")

    details = store.run_details(run_id)
    assert details is not None
    assert details["steps"][-1]["status"] == "failed"


def test_reviewer_runs_only_fixed_read_only_gates(tmp_path: Path) -> None:
    from runtime.agents import ReadOnlyReviewAgent

    context = SimpleNamespace(
        inspect_diff=lambda: "diff",
        run_verification=lambda: "Verification: PASS",
        review_diff=lambda: '{"verdict": "pass"}',
    )
    reviewer = ReadOnlyReviewAgent(
        name="reviewer",
        description="Read-only review",
        context=context,
    )

    result = reviewer("Review README.md", {"ignored": True})

    assert result == 'diff\n\nVerification: PASS\n\n{"verdict": "pass"}'
    assert not hasattr(reviewer, "python_executor")


def test_reviewer_reports_review_failure_without_retrying() -> None:
    from runtime.agents import ReadOnlyReviewAgent

    def failed_review() -> str:
        raise RuntimeError("invalid structured response")

    context = SimpleNamespace(
        inspect_diff=lambda: "diff",
        run_verification=lambda: "Verification: PASS",
        review_diff=failed_review,
    )
    reviewer = ReadOnlyReviewAgent(
        name="reviewer",
        description="Read-only review",
        context=context,
    )

    result = reviewer("Review README.md")

    assert result == (
        "diff\n\nVerification: PASS\n\n"
        "Review unavailable: invalid structured response"
    )


def test_state_store_records_run_and_verification(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Add a feature",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    store.register_agent(
        run_id,
        role="explorer",
        skill="explore-repository",
        model_route="local-plan",
    )
    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="ok",
        duration_ms=1.5,
    )
    store.log_tool_call(
        run_id,
        agent_role="implementer",
        tool_name="apply_atomic_edit",
        arguments={},
        output="EditorError: missing match",
        status="error",
        duration_ms=1.0,
    )
    store.update_run(run_id, status="awaiting_approval")

    details = store.run_details(run_id)

    assert details is not None
    assert details["status"] == "awaiting_approval"
    assert details["agents"][0]["role"] == "explorer"
    assert details["verification"][0]["passed"] == 1
    assert store.tool_call_error_count(run_id) == 1
    assert store.tool_call_error_count(run_id, tool_name="apply_atomic_edit") == 1


def test_no_diff_skips_verification_and_ignores_manager_success_claim(
    tmp_path: Path,
) -> None:
    from runtime.orchestrator import AgentOrchestrator, OrchestratorConfig

    repository = tmp_path / "repository"
    worktree_path = tmp_path / "worktree"
    repository.mkdir()
    worktree_path.mkdir()
    orchestrator = AgentOrchestrator(OrchestratorConfig(repository=repository))
    bundle = SimpleNamespace(
        manager=SimpleNamespace(
            run=lambda *_args, **_kwargs: "Task completed successfully."
        )
    )

    with (
        patch.object(orchestrator, "_service_preflight"),
        patch.object(orchestrator, "_run_identity", return_value=("abc", "def", "ghi")),
        patch("runtime.orchestrator.current_branch", return_value="main"),
        patch(
            "runtime.orchestrator.create_worktree",
            return_value=Worktree(worktree_path, "agent/test", "main"),
        ),
        patch("runtime.orchestrator.discover_skills", return_value={}),
        patch("runtime.orchestrator.build_agent_bundle", return_value=bundle),
        patch.object(ToolContext, "inspect_diff", return_value="No uncommitted diff."),
        patch.object(ToolContext, "run_verification") as run_verification,
        patch.object(ToolContext, "review_diff") as review_diff,
    ):
        summary = orchestrator.run("Make one exact change")

    assert summary.status == "no_changes"
    assert summary.verification_passed is False
    assert "Task completed successfully" not in summary.result
    assert "did not produce a reviewable diff" in summary.result
    assert "verification skipped" in summary.result.lower()
    run_verification.assert_not_called()
    review_diff.assert_not_called()


def test_no_diff_with_editor_error_requires_attention() -> None:
    from runtime.orchestrator import determine_run_status

    status = determine_run_status(
        verification_passed=False,
        has_diff=False,
        review_verdict=None,
        has_scope_violations=False,
        editor_error_count=1,
    )

    assert status == "needs_attention"


def test_rejected_editor_attempt_forces_needs_attention() -> None:
    from runtime.orchestrator import determine_run_status

    status = determine_run_status(
        verification_passed=True,
        has_diff=True,
        review_verdict="pass",
        has_scope_violations=False,
        editor_error_count=1,
    )

    assert status == "needs_attention"


def test_final_review_failure_requires_attention_without_stale_verdict() -> None:
    from runtime.orchestrator import collect_final_review, determine_run_status

    def failed_review() -> str:
        raise RuntimeError("invalid structured response")

    context = SimpleNamespace(
        review_diff=failed_review,
        last_review_verdict="pass",
    )

    output, verdict = collect_final_review(context)
    status = determine_run_status(
        verification_passed=True,
        has_diff=True,
        review_verdict=verdict,
        has_scope_violations=False,
        editor_error_count=0,
    )

    assert output == "Review unavailable: invalid structured response"
    assert verdict is None
    assert status == "needs_attention"


def test_tool_context_rejects_paths_outside_worktree(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Test path safety",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=worktree_path / "TASK.md",
    )

    try:
        context.read_file("../outside.txt")
    except ValueError as exc:
        assert "escapes the worktree" in str(exc)
    else:
        raise AssertionError("Unsafe path was accepted")


def _initialize_edit_test_repository(worktree_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=worktree_path,
        check=True,
    )
    subprocess.run(
        ["git", "add", "README.md", "TASK.md"], cwd=worktree_path, check=True
    )
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=worktree_path, check=True)


def test_atomic_editor_rejects_requests_outside_predeclared_scope(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "README.md").write_text("before\n", encoding="utf-8")
    (worktree_path / "other.py").write_text("value = 1\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        allowed_edit_paths=frozenset({"README.md"}),
    )

    with (
        patch("runtime.tools.request_and_apply") as request_and_apply,
        pytest.raises(RuntimeError, match="outside the predeclared scope: other.py"),
    ):
        context.apply_atomic_edit("Change another file", "other.py")

    request_and_apply.assert_not_called()
    assert context.scope_violations == {"other.py"}
    assert store.tool_call_error_count(run_id, tool_name="apply_atomic_edit") == 1


def test_prepared_atomic_edits_use_native_editor_and_existing_audit_name(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        agent_role="implementer",
        allowed_edit_paths=frozenset({"README.md"}),
    )

    result = context.apply_prepared_atomic_edits(
        "Replace the exact line",
        "README.md",
        [
            SimpleNamespace(
                model_dump=lambda: {
                    "path": "README.md",
                    "old_text": "before",
                    "new_text": "after",
                }
            )
        ],
    )

    assert result == "Atomic edit applied to: README.md"
    assert editable.read_text(encoding="utf-8") == "after\n"
    details = store.run_details(run_id)
    assert details is not None
    tool_call = details["tool_calls"][-1]
    assert tool_call["tool_name"] == "apply_atomic_edit"
    assert tool_call["agent_role"] == "implementer"
    assert tool_call["status"] == "success"
    assert json.loads(tool_call["arguments"])["source"] == "dspy-implementer"


def test_prepared_atomic_edits_fail_closed_on_invalid_shape(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "README.md").write_text("before\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        agent_role="implementer",
    )

    with pytest.raises(EditorError, match="Each edit must contain"):
        context.apply_prepared_atomic_edits(
            "Replace the exact line",
            "README.md",
            [{"path": "README.md", "old_text": "before"}],
        )

    assert store.tool_call_error_count(run_id, tool_name="apply_atomic_edit") == 1


def test_atomic_editor_normalizes_approved_scope_aliases(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        allowed_edit_paths=frozenset({"README.md"}),
    )

    def fake_editor(**_: object) -> list[str]:
        editable.write_text("after\n", encoding="utf-8")
        return ["./README.md"]

    with patch("runtime.tools.request_and_apply", side_effect=fake_editor):
        result = context.apply_atomic_edit("Change one line", "./README.md")

    assert result == "Atomic edit applied to: ./README.md"
    assert context.scope_violations == set()


def test_atomic_editor_rejects_success_without_an_edit(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("unchanged\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    with (
        patch(
            "runtime.tools.request_and_apply",
            return_value=["README.md"],
        ),
        pytest.raises(RuntimeError, match="did not change an editable file"),
    ):
        context.apply_atomic_edit("Add one sentence", "README.md")


def test_atomic_editor_rejects_changes_outside_editable_scope(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    editable = worktree_path / "README.md"
    editable.write_text("before\n", encoding="utf-8")
    task_file = worktree_path / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    _initialize_edit_test_repository(worktree_path)
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Edit README.md",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    def fake_editor(**_: object) -> list[str]:
        editable.write_text("after\n", encoding="utf-8")
        (worktree_path / "unexpected.py").write_text("", encoding="utf-8")
        return ["README.md"]

    with (
        patch("runtime.tools.request_and_apply", side_effect=fake_editor),
        pytest.raises(RuntimeError, match="outside the editable scope: unexpected.py"),
    ):
        context.apply_atomic_edit("Change one line", "README.md")


def test_review_diff_uses_project_python(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Review a diff",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    run_dir = worktree_path / ".local-coder" / "runs" / run_id
    run_dir.mkdir(parents=True)
    task_file = run_dir / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
    )

    store.add_verification(
        run_id,
        command="make verify",
        passed=True,
        output="149 passed",
        duration_ms=1.0,
    )

    def successful_review(*_: object, **__: object) -> SimpleNamespace:
        (run_dir / "REVIEW.json").write_text(
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "The audited diff matches the task.",
                    "issues": [],
                    "unrelated_changes": [],
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "REVIEW.metrics.json").write_text(
            json.dumps(
                {
                    "route": "local-review",
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "metadata": {
                        "source": "dspy-reviewer",
                        "program": "ReviewerProgram",
                        "adapter": "JSONAdapter",
                        "status": "success",
                    },
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="reviewed", stderr="")

    with (
        patch("runtime.tools.command", side_effect=successful_review) as command,
        patch("runtime.tools.collect_changed_paths", return_value={"README.md"}),
        patch("runtime.tools.collect_uncommitted_diff", return_value="diff"),
    ):
        assert context.review_diff() == "reviewed"

    assert command.call_args.args[0][0] == local_coder.sys.executable
    assert command.call_args.args[0][1] == "./review-diff.py"
    assert context.last_review_verdict == "pass"
    details = store.run_details(run_id)
    assert details is not None
    traces = [
        json.loads(artifact["content"])
        for artifact in details["artifacts"]
        if artifact["kind"] == "dspy_trace"
    ]
    assert traces[-1]["role"] == "reviewer"
    assert traces[-1]["inputs"]["verification_passed"] is True
    assert traces[-1]["output"]["verdict"] == "pass"


def test_review_diff_discards_stale_verdict_after_failure(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Review a diff",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    run_dir = worktree_path / ".local-coder" / "runs" / run_id
    run_dir.mkdir(parents=True)
    output_path = run_dir / "REVIEW.json"
    output_path.write_text('{"verdict": "pass"}', encoding="utf-8")
    task_file = run_dir / "TASK.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        worktree=Worktree(worktree_path, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        last_review_verdict="pass",
    )

    with (
        patch(
            "runtime.tools.command",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Review error: invalid structured response",
            ),
        ),
        pytest.raises(RuntimeError, match="invalid structured response"),
    ):
        context.review_diff()

    assert context.last_review_verdict is None
    assert not output_path.exists()
    details = store.run_details(run_id)
    assert details is not None
    assert details["tool_calls"][-1]["status"] == "error"


def test_model_registry_exposes_role_aliases() -> None:
    registry = ModelRegistry()

    assert set(registry.routes) == {
        "local-fast",
        "local-plan",
        "local-review",
    }


def test_untracked_files_are_included_in_diff(tmp_path: Path) -> None:
    import subprocess

    from runtime.tools import collect_uncommitted_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    (tmp_path / "new_file.py").write_text("value = 1\n", encoding="utf-8")

    diff = collect_uncommitted_diff(tmp_path)

    assert "new_file.py" in diff
    assert "+value = 1" in diff


def test_untracked_symlinks_are_included_in_diff(tmp_path: Path) -> None:
    import subprocess

    from runtime.tools import collect_uncommitted_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    target = tmp_path / "environment"
    target.mkdir()
    (tmp_path / "linked-environment").symlink_to(target, target_is_directory=True)

    diff = collect_uncommitted_diff(tmp_path)

    assert "new file mode 120000" in diff
    assert "linked-environment" in diff
    assert f"+{target}" in diff


def test_staged_empty_files_are_included_in_diff(tmp_path: Path) -> None:
    import subprocess

    from runtime.tools import collect_uncommitted_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    (tmp_path / "staged_empty.py").touch()
    subprocess.run(["git", "add", "staged_empty.py"], cwd=tmp_path, check=True)

    diff = collect_uncommitted_diff(tmp_path)

    assert "staged_empty.py" in diff


def test_primary_actor_documents_exist() -> None:
    assert (ROOT / "AGENTS.md").is_file()
    assert (ROOT / "ROADMAP.md").is_file()
    assert (ROOT / "docs" / "HANDOFF.md").is_file()
    assert (ROOT / "docs" / "ARCHITECTURE.md").is_file()
    assert (ROOT / "docs" / "PIPELINE.md").is_file()
    assert (ROOT / "docs" / "CONVENTIONS.md").is_file()


def test_create_worktree_shares_virtualenv(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Local Coder Test"],
        cwd=root,
        check=True,
    )
    (root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (root / ".venv").mkdir()
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)

    worktree = create_worktree(root, run_id="abc123", task="Share environment")
    try:
        target = worktree.path / ".venv"
        assert target.is_symlink()
        assert target.resolve() == (root / ".venv").resolve()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree.path,
            check=True,
            text=True,
            capture_output=True,
        )
        assert status.stdout == ""
    finally:
        remove_worktree(root, worktree, force=True)


def test_explorer_completion_claim_falls_back_to_raw_evidence() -> None:
    from runtime.agents import _ground_evidence_response

    evidence = ["1: The old phrase is still present."]
    result = _ground_evidence_response(
        role="explorer",
        response="The requested change has been made successfully.",
        evidence=evidence,
    )

    assert "performed no edits" in result
    assert evidence[0] in result
    assert "has been made successfully" not in result


def test_planner_response_is_not_filtered_as_explorer_completion_claim() -> None:
    from runtime.agents import _ground_evidence_response

    response = "The plan has been completed."
    assert (
        _ground_evidence_response(
            role="planner", response=response, evidence=["evidence"]
        )
        == response
    )


def test_implementation_report_uses_successful_audited_editor_call() -> None:
    from runtime.agents import _implementation_report

    report, succeeded = _implementation_report(
        [
            {
                "tool_name": "apply_atomic_edit",
                "status": "success",
                "output": "Changed profiles/README.md",
            }
        ]
    )

    assert succeeded is True
    assert report == "Implementation succeeded: Changed profiles/README.md"


def test_implementation_report_rejects_generated_success_without_editor_call() -> None:
    from runtime.agents import _implementation_report

    report, succeeded = _implementation_report([])

    assert succeeded is False
    assert report == "Implementation failed: no apply_atomic_edit call was recorded."


def test_implementation_report_rejects_multiple_editor_calls() -> None:
    from runtime.agents import _implementation_report

    report, succeeded = _implementation_report(
        [
            {
                "tool_name": "apply_atomic_edit",
                "status": "success",
                "output": "first",
            },
            {
                "tool_name": "apply_atomic_edit",
                "status": "success",
                "output": "second",
            },
        ]
    )

    assert succeeded is False
    assert "expected exactly one successful" in report


def test_live_e2e_schema_is_strict_and_scoped() -> None:
    from runtime.live_e2e import EXPECTED_FILE, edit_schema

    payload = edit_schema()
    assert payload["additionalProperties"] is False
    item = payload["properties"]["edits"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["path"]["enum"] == [EXPECTED_FILE]


def test_status_requires_dspy_dependency(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(local_coder, "llama_server_is_healthy", return_value=True),
        patch.object(local_coder, "litellm_is_available", return_value=True),
        patch.object(
            local_coder.importlib.util,
            "find_spec",
            side_effect=lambda name: object() if name == "smolagents" else None,
        ),
        patch.object(
            local_coder,
            "command_output",
            side_effect=[(0, "main"), (0, "")],
        ),
    ):
        assert local_coder.handle_status(SimpleNamespace()) == 1

    output = capsys.readouterr().out
    assert "smolagents          OK" in output
    assert "DSPy                NOT INSTALLED" in output


def test_dspy_reviewer_cannot_pass_failed_verification() -> None:
    def runner(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            verdict="pass",
            summary="The change appears correct.",
            issues=[],
            unrelated_changes=[],
        )

    with pytest.raises(
        review_diff_cli.ReviewError,
        match="cannot pass a change when verification failed",
    ):
        review_diff_cli.call_reviewer(
            model="local-review",
            task="Update README.md",
            changed_files=["README.md"],
            diff="diff",
            verification_passed=False,
            verification_output="Verification: FAIL",
            reviewer_runner=runner,
        )


def test_dspy_reviewer_cannot_pass_with_reported_issues() -> None:
    def runner(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            verdict="pass",
            summary="The change has an issue.",
            issues=["README.md: unrelated wording change"],
            unrelated_changes=[],
        )

    with pytest.raises(
        review_diff_cli.ReviewError,
        match="cannot pass a change with reported issues",
    ):
        review_diff_cli.call_reviewer(
            model="local-review",
            task="Update README.md",
            changed_files=["README.md"],
            diff="diff",
            verification_passed=True,
            verification_output="Verification: PASS",
            reviewer_runner=runner,
        )


def test_dspy_explorer_prediction_rejects_missing_repository_path(
    tmp_path: Path,
) -> None:
    from runtime.agents import _format_explorer_prediction

    prediction = SimpleNamespace(
        findings=["A relevant file was identified."],
        relevant_files=["missing.py"],
        constraints=[],
        unresolved_questions=[],
    )

    with pytest.raises(RuntimeError, match="references a missing file"):
        _format_explorer_prediction(prediction, worktree=tmp_path)


def test_dspy_planner_prediction_rejects_more_than_two_editable_files(
    tmp_path: Path,
) -> None:
    from runtime.agents import _format_planner_prediction

    for name in ("one.py", "two.py", "three.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")
    prediction = SimpleNamespace(
        instruction="Update three files.",
        editable_files=["one.py", "two.py", "three.py"],
        acceptance_criteria=["Run make verify."],
        depends_on=[],
    )

    with pytest.raises(RuntimeError, match="planner editable_files"):
        _format_planner_prediction(prediction, worktree=tmp_path)


def test_dspy_evidence_failure_is_audited_without_model_prose(
    tmp_path: Path,
) -> None:
    from runtime.agents import ReadOnlyEvidenceAgent

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    task_file = repository / "TASK.md"
    task_file.write_text("Inspect README.md.\n", encoding="utf-8")
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="Inspect README.md",
        mode="agentic",
        repository=repository,
        base_branch="main",
    )
    context = ToolContext(
        root=repository,
        worktree=Worktree(repository, "agent/test", "main"),
        run_id=run_id,
        state=store,
        task_file=task_file,
        agent_role="explorer",
    )

    def malformed_runner(**_: object) -> SimpleNamespace:
        return SimpleNamespace(
            findings=[],
            relevant_files=["README.md"],
            constraints=[],
            unresolved_questions=[],
        )

    agent = ReadOnlyEvidenceAgent(
        name="explorer",
        description="Read-only evidence",
        activate_skill=lambda: SimpleNamespace(),
        context=context,
        model_route="local-plan",
        lm_factory=lambda: SimpleNamespace(model="openai/local-plan"),
        program_runner=malformed_runner,
        program_name="ExplorerProgram",
        state=store,
        run_id=run_id,
    )

    with pytest.raises(RuntimeError, match="explorer findings"):
        agent("Inspect README.md")

    details = store.run_details(run_id)
    assert details is not None
    assert details["steps"][-1]["status"] == "failed"
    metadata = json.loads(details["model_metrics"][-1]["metadata"])
    assert metadata["status"] == "error"
    assert metadata["source"] == "dspy-explorer"
