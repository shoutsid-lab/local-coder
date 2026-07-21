"""Construct the role-separated smolagents hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ModelRegistry
from .skills import Skill
from .state import StateStore
from .tools import ToolContext, build_smol_tools


@dataclass(frozen=True)
class AgentBundle:
    """The manager and its managed role agents."""

    manager: Any
    managed: tuple[Any, ...]


def _build_agent(
    *,
    role: str,
    skill: Skill,
    context: ToolContext,
    models: ModelRegistry,
    state: StateStore,
    run_id: str,
) -> Any:
    try:
        from smolagents import CodeAgent
    except ImportError as exc:
        raise RuntimeError(
            "smolagents is not installed. Run `make agent-install`."
        ) from exc

    role_context = ToolContext(
        root=context.root,
        worktree=context.worktree,
        run_id=context.run_id,
        state=context.state,
        task_file=context.task_file,
        agent_role=role,
    )
    state.register_agent(
        run_id,
        role=role,
        skill=skill.name,
        model_route=skill.model,
    )
    return CodeAgent(
        tools=build_smol_tools(role_context, skill.tools),
        model=models.build(skill.model),
        instructions=skill.instructions,
        max_steps=skill.max_steps,
        name=role,
        description=skill.description,
        provide_run_summary=True,
        add_base_tools=False,
        additional_authorized_imports=[],
        use_structured_outputs_internally=False,
    )


def build_agent_bundle(
    *,
    skills: dict[str, Skill],
    context: ToolContext,
    models: ModelRegistry,
    state: StateStore,
    run_id: str,
    manager_max_steps: int = 12,
) -> AgentBundle:
    """Build explorer, planner, implementer, repairer, reviewer, and manager."""
    try:
        from smolagents import CodeAgent
    except ImportError as exc:
        raise RuntimeError(
            "smolagents is not installed. Run `make agent-install`."
        ) from exc

    role_skills = {
        "explorer": "explore-repository",
        "planner": "plan-change",
        "implementer": "atomic-implementation",
        "repairer": "test-and-repair",
        "reviewer": "review-change",
    }
    missing = set(role_skills.values()) - skills.keys()
    if missing:
        raise ValueError(f"Required skills are missing: {sorted(missing)}")

    managed = tuple(
        _build_agent(
            role=role,
            skill=skills[skill_name],
            context=context,
            models=models,
            state=state,
            run_id=run_id,
        )
        for role, skill_name in role_skills.items()
    )

    state.register_agent(
        run_id,
        role="orchestrator",
        skill="orchestrate",
        model_route="local-plan",
    )
    manager = CodeAgent(
        tools=build_smol_tools(
            context, ("git_status", "inspect_diff", "run_verification")
        ),
        model=models.build("local-plan"),
        managed_agents=list(managed),
        instructions=(
            "Coordinate the managed agents in this order when appropriate: "
            "explorer, planner, implementer, repairer, reviewer. Do not edit files "
            "yourself. Require atomic edits, deterministic verification after edits, "
            "and a final read-only review. Stop rather than weakening tests or task "
            "requirements. The worktree must remain uncommitted for human approval."
        ),
        max_steps=manager_max_steps,
        planning_interval=3,
        add_base_tools=False,
        additional_authorized_imports=[],
        use_structured_outputs_internally=False,
    )
    return AgentBundle(manager=manager, managed=managed)
