"""Offline construction smoke test for the role-separated agent hierarchy."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .agents import build_agent_bundle
from .dspy_lm import build_dspy_lm
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
        reviewer_lm = build_dspy_lm("local-review")
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
    if reviewer_lm.model != "openai/local-review":
        raise RuntimeError(f"Unexpected DSPy reviewer route: {reviewer_lm.model}")
    print("Agent hierarchy: OK")
    print(f"Manager: {bundle.manager.__class__.__name__}")
    print(
        f"Managed agents: {', '.join(names)} "
        "(read-only evidence/review adapters and CodeAgent workers)"
    )
    print(
        f"DSPy reviewer: {reviewer_program.__class__.__name__} "
        f"-> {reviewer_lm.model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
