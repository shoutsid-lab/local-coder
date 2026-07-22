"""Offline construction smoke test for the role-separated agent hierarchy."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .agents import build_agent_bundle
from .dspy_lm import build_dspy_lm
from .dspy_programs.explorer import ExplorerProgram
from .dspy_programs.implementer import ImplementerProgram
from .dspy_programs.planner import PlannerProgram
from .dspy_programs.reviewer import ReviewerProgram
from .models import ModelRegistry
from .skills_loader import discover_skills
from .state import StateStore
from .tools import ToolContext, Worktree


def main() -> int:
    """Instantiate the manager and managed agents without calling a model."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="local-coder-smoke-") as temporary:
        temporary_path = Path(temporary)
        state = StateStore(temporary_path / "agent.db")
        task_file = temporary_path / "TASK.md"
        task_file.write_text(
            "# Agent Task\n\nSmoke-test construction only.\n",
            encoding="utf-8",
        )
        run_id = state.create_run(
            task="Agent hierarchy smoke test",
            mode="agentic",
            repository=root,
            base_branch="main",
        )
        context = ToolContext(
            root=root,
            worktree=Worktree(root, "agent/smoke", "main"),
            run_id=run_id,
            state=state,
            task_file=task_file,
            agent_role="orchestrator",
        )
        explorer_lm = build_dspy_lm("local-plan")
        planner_lm = build_dspy_lm("local-plan")
        implementer_lm = build_dspy_lm("local-fast")
        reviewer_lm = build_dspy_lm("local-review")
        explorer_program = ExplorerProgram()
        planner_program = PlannerProgram()
        implementer_program = ImplementerProgram()
        reviewer_program = ReviewerProgram()
        bundle = build_agent_bundle(
            skills=discover_skills(root / ".local-coder" / "skills"),
            context=context,
            models=ModelRegistry(),
            state=state,
            run_id=run_id,
        )

    names = [agent.name for agent in bundle.managed]
    expected = ["explorer", "planner", "implementer", "repairer", "reviewer"]
    if names != expected:
        raise RuntimeError(f"Unexpected managed-agent order: {names}")
    if explorer_lm.model != "openai/local-plan":
        raise RuntimeError(f"Unexpected DSPy explorer route: {explorer_lm.model}")
    if planner_lm.model != "openai/local-plan":
        raise RuntimeError(f"Unexpected DSPy planner route: {planner_lm.model}")
    if implementer_lm.model != "openai/local-fast":
        raise RuntimeError(f"Unexpected DSPy implementer route: {implementer_lm.model}")
    if reviewer_lm.model != "openai/local-review":
        raise RuntimeError(f"Unexpected DSPy reviewer route: {reviewer_lm.model}")
    if bundle.managed[0].program_name != "ExplorerProgram":
        raise RuntimeError("Explorer is not bound to the DSPy explorer program.")
    if bundle.managed[1].program_name != "PlannerProgram":
        raise RuntimeError("Planner is not bound to the DSPy planner program.")
    if bundle.managed[2].program_name != "ImplementerProgram":
        raise RuntimeError("Implementer is not bound to the DSPy implementer program.")
    print("Agent hierarchy: OK")
    print(f"Manager: {bundle.manager.__class__.__name__}")
    print(
        f"Managed agents: {', '.join(names)} "
        "(DSPy role adapters, read-only reviewer, and repair CodeAgent)"
    )
    print(
        f"DSPy explorer: {explorer_program.__class__.__name__} "
        f"-> {explorer_lm.model}"
    )
    print(
        f"DSPy planner: {planner_program.__class__.__name__} " f"-> {planner_lm.model}"
    )
    print(
        f"DSPy implementer: {implementer_program.__class__.__name__} "
        f"-> {implementer_lm.model}"
    )
    print(
        f"DSPy reviewer: {reviewer_program.__class__.__name__} "
        f"-> {reviewer_lm.model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
