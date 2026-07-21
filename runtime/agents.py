"""Construct the role-separated smolagents hierarchy."""

from __future__ import annotations

import re
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


@dataclass
class ReadOnlyEvidenceAgent:
    """Gather recorded evidence, then request one text-only role response."""

    name: str
    description: str
    skill: Skill
    context: ToolContext
    model: Any

    def __call__(
        self,
        task: str,
        additional_args: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        del additional_args
        authoritative_task = self.context.task_file.read_text(encoding="utf-8")
        named_files = list(
            dict.fromkeys(
                re.findall(
                    r"[\w./-]+\.[A-Za-z0-9]+",
                    f"{authoritative_task}\n{task}",
                )
            )
        )
        evidence: list[str] = []
        for path in named_files[:3]:
            try:
                evidence.append(self.context.read_file(path, 1, 240))
            except (FileNotFoundError, ValueError):
                continue
        if not evidence:
            evidence.append(self.context.list_files("*"))

        response = self.model.generate(
            [
                {
                    "role": "system",
                    "content": (
                        f"{self.skill.instructions}\n\n"
                        "Return concise plain text only. You have no tools and must "
                        "not emit code, tool calls, or editing instructions. Base "
                        "the response only on the supplied repository evidence."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Authoritative task:\n{authoritative_task}\n\n"
                        f"Delegated task:\n{task}\n\n"
                        f"Repository evidence:\n{'\n\n'.join(evidence)}"
                    ),
                },
            ],
            tools_to_call_from=None,
        )
        return str(response.content)


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

    class ManagedCodeAgent(CodeAgent):
        """Accept the small model's positional additional-arguments convention."""

        def __call__(
            self,
            task: str,
            additional_args: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            authoritative_task = context.task_file.read_text(encoding="utf-8")
            named_files = re.findall(r"[\w./-]+\.[A-Za-z0-9]+", task)
            first_file = named_files[0] if named_files else None
            if role == "implementer" and first_file:
                first_action = (
                    "Your first action must call delegate_aider exactly once, using "
                    f"instruction={task!r} and editable_files={first_file!r}."
                )
            elif role == "reviewer":
                first_action = "Your first action must call inspect_diff()."
            else:
                first_action = "Your first action must call run_verification()."
            constrained_task = (
                f"{skill.instructions}\n\n"
                "Every action must be valid Python inside <code> tags. Call your "
                "allowed tools with Python keyword arguments before answering. When "
                "finished, call the "
                "final_answer tool with exactly one argument named answer. Put any "
                "requested report headings inside that single answer string; never "
                "pass task_outcome_short, task_outcome_detailed, or "
                "additional_context as arguments. Do not narrate or claim success "
                f"before the first tool call. {first_action}\n\n"
                f"Authoritative task:\n{authoritative_task}\n\n"
                f"Delegated task:\n{task}"
            )
            return self.run(
                constrained_task,
                reset=True,
                additional_args=additional_args,
                **kwargs,
            )

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
    if role in {"explorer", "planner"}:
        return ReadOnlyEvidenceAgent(
            name=role,
            description=skill.description,
            skill=skill,
            context=role_context,
            model=models.build(skill.model),
        )
    return ManagedCodeAgent(
        tools=build_smol_tools(role_context, skill.tools),
        model=models.build(skill.model),
        instructions=skill.instructions,
        max_steps=skill.max_steps,
        name=role,
        description=skill.description,
        provide_run_summary=False,
        add_base_tools=False,
        additional_authorized_imports=[],
        use_structured_outputs_internally=False,
        executor_kwargs={"timeout_seconds": 600},
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
            "Every action must be valid Python inside <code> tags. Call each managed "
            "agent with exactly one positional string argument, for example "
            "explorer('Inspect README.md for the requested change'). Do not pass a "
            "second dictionary argument. Define values in the executed code block or "
            "use string literals; planning examples are not execution state. "
            "Your own tools are only git_status, inspect_diff, and run_verification; "
            "never call a managed agent's repository or editing tools yourself. "
            "Coordinate the managed agents in this order when appropriate: "
            "explorer, planner, implementer, repairer, reviewer. Do not edit files "
            "yourself. Require atomic edits, deterministic verification after edits, "
            "and inspect the actual verification output. Never call the repairer "
            "unless run_verification literally returned Verification: FAIL. Require "
            "a final read-only review. Stop rather than weakening tests or task "
            "requirements. The worktree must remain uncommitted for explicit approval."
        ),
        max_steps=manager_max_steps,
        planning_interval=None,
        add_base_tools=False,
        additional_authorized_imports=[],
        use_structured_outputs_internally=False,
        executor_kwargs={"timeout_seconds": 600},
    )
    return AgentBundle(manager=manager, managed=managed)
