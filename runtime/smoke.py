"""Offline construction smoke test for the role-separated agent hierarchy."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .agents import build_agent_bundle
from .dspy_lm import build_dspy_lm_with_profile
from .dspy_programs.explorer import ExplorerProgram
from .dspy_programs.implementer import ImplementerProgram
from .dspy_programs.planner import PlannerProgram
from .dspy_programs.repairer import RepairerProgram
from .dspy_programs.reviewer import ReviewerProgram
from .models import ModelRegistry
from .role_profiles import role_generation_profile, role_route
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
        explorer_lm = build_dspy_lm_with_profile(
            role_route("explorer"), role_generation_profile("explorer")
        )
        planner_lm = build_dspy_lm_with_profile(
            role_route("planner"), role_generation_profile("planner")
        )
        implementer_lm = build_dspy_lm_with_profile(
            role_route("implementer"), role_generation_profile("implementer")
        )
        repairer_lm = build_dspy_lm_with_profile(
            role_route("repairer"), role_generation_profile("repairer")
        )
        reviewer_lm = build_dspy_lm_with_profile(
            role_route("reviewer"), role_generation_profile("reviewer")
        )
        explorer_program = ExplorerProgram()
        planner_program = PlannerProgram()
        implementer_program = ImplementerProgram()
        repairer_program = RepairerProgram()
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
    role_models = {
        "explorer": explorer_lm.model,
        "planner": planner_lm.model,
        "implementer": implementer_lm.model,
        "repairer": repairer_lm.model,
        "reviewer": reviewer_lm.model,
    }
    for role, model in role_models.items():
        expected_model = f"openai/{role_route(role)}"
        if model != expected_model:
            raise RuntimeError(
                f"Unexpected DSPy {role} route: {model}; expected {expected_model}"
            )
    if bundle.managed[0].program_name != "ExplorerProgram":
        raise RuntimeError("Explorer is not bound to the DSPy explorer program.")
    if bundle.managed[1].program_name != "PlannerProgram":
        raise RuntimeError("Planner is not bound to the DSPy planner program.")
    if bundle.managed[2].program_name != "ImplementerProgram":
        raise RuntimeError("Implementer is not bound to the DSPy implementer program.")
    if bundle.managed[3].program_name != "RepairerProgram":
        raise RuntimeError("Repairer is not bound to the DSPy repairer program.")
    print("Agent hierarchy: OK")
    print(f"Manager: {bundle.manager.__class__.__name__}")
    print(
        f"Managed agents: {', '.join(names)} "
        "(DSPy specialist adapters and a read-only reviewer boundary)"
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
        f"DSPy repairer: {repairer_program.__class__.__name__} "
        f"-> {repairer_lm.model}"
    )
    print(
        f"DSPy reviewer: {reviewer_program.__class__.__name__} "
        f"-> {reviewer_lm.model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
