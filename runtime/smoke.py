"""Offline construction smoke test for the role-separated agent hierarchy."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .agents import build_agent_bundle
from .models import ModelRegistry
from .skills import discover_skills
from .state import StateStore
from .tools import ToolContext, Worktree


def main() -> int:
    """Instantiate the manager and managed agents without calling a model."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="local-coder-smoke-") as temporary:
        state = StateStore(Path(temporary) / "agent.db")
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
            task_file=root / "TASK.md",
            agent_role="orchestrator",
        )
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
    print("Agent hierarchy: OK")
    print(f"Manager: {bundle.manager.__class__.__name__}")
    print(f"Managed agents: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
