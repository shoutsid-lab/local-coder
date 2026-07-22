import inspect
import importlib.util
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
    from smolagents import CodeAgent

    from runtime.agents import (
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
    bundle = build_agent_bundle(
        skills=discover_skills(ROOT / ".local-coder" / "skills"),
        context=context,
        models=ModelRegistry(),
        state=store,
        run_id=run_id,
    )

    parameters = inspect.signature(bundle.managed[0].__call__).parameters

    assert all(isinstance(agent, ReadOnlyEvidenceAgent) for agent in bundle.managed[:2])
    assert all(isinstance(agent, CodeAgent) for agent in bundle.managed[2:4])
    assert isinstance(bundle.managed[4], ReadOnlyReviewAgent)
    assert all(
        agent.python_executor.timeout_seconds == 600 for agent in bundle.managed[2:4]
    )
    assert "additional_args" in parameters
    assert bundle.manager.python_executor.timeout_seconds == 600
    assert bundle.manager.planning_interval is None

    explorer = bundle.managed[0]
    with patch.object(
        explorer.model,
        "generate",
        return_value=SimpleNamespace(content="evidence summary"),
    ) as generate:
        assert explorer("Inspect README.md", {"request": "context"}) == (
            "evidence summary"
        )
    messages = generate.call_args.args[0]
    assert "You have no tools" in messages[0]["content"]
    assert "# local-coder" in messages[1]["content"]
    assert generate.call_args.kwargs["tools_to_call_from"] is None
    details = store.run_details(run_id)
    assert details is not None
    assert details["tool_calls"][0]["agent_role"] == "explorer"
    assert details["tool_calls"][0]["tool_name"] == "read_file"

    implementer = bundle.managed[2]
    with patch.object(implementer, "run", return_value="done") as run:
        implementer("Replace one sentence in README.md")
    assert "apply_atomic_edit exactly once" in run.call_args.args[0]
    assert "editable_files='README.md'" in run.call_args.args[0]


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
    assert store.tool_call_error_count(
        run_id, tool_name="apply_atomic_edit"
    ) == 1


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

    def successful_review(*_: object, **__: object) -> SimpleNamespace:
        (run_dir / "REVIEW.json").write_text(
            '{"verdict": "pass"}',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="reviewed", stderr="")

    with patch(
        "runtime.tools.command",
        side_effect=successful_review,
    ) as command:
        assert context.review_diff() == "reviewed"

    assert command.call_args.args[0][0] == local_coder.sys.executable
    assert command.call_args.args[0][1] == "./review-diff.py"
    assert context.last_review_verdict == "pass"


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


def test_codex_handoff_documents_exist() -> None:
    assert (ROOT / "AGENTS.md").is_file()
    assert (ROOT / "HANDOFF.md").is_file()
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
