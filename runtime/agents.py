"""Construct the role-separated smolagents hierarchy."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .dspy_lm import build_dspy_lm
from .editor import load_editable_files
from .models import AuditedModel, ModelRegistry, ModelUsageBudget
from .skills import (
    Skill,
    SkillCatalog,
    activate_skill,
    runtime_skill_config,
)
from .state import StateStore
from .tools import ToolContext, build_smol_tools

_COMPLETION_CLAIM = re.compile(
    r"\b(?:task\s+)?(?:has|have|was|were)?\s*(?:been\s+)?"
    r"(?:completed|implemented|updated|changed|replaced|made)\b|\bsuccessfully\b",
    re.IGNORECASE,
)


def _ground_evidence_response(*, role: str, response: str, evidence: list[str]) -> str:
    """Return read-only evidence without allowing false completion claims."""
    if role == "explorer" and _COMPLETION_CLAIM.search(response):
        return (
            "Read-only evidence report; the explorer performed no edits and this "
            "is not a completion status.\n\n" + "\n\n".join(evidence)
        )
    return response


def _prediction_value(prediction: Any, name: str) -> Any:
    """Read one typed DSPy prediction field from object or mapping output."""
    if isinstance(prediction, Mapping):
        return prediction[name]
    return getattr(prediction, name)


def _prediction_usage(
    prediction: Any,
    route: str,
) -> tuple[int | None, int | None]:
    """Return prompt and completion usage attached by DSPy's tracker."""
    get_usage = getattr(prediction, "get_lm_usage", None)
    if not callable(get_usage):
        return None, None
    usage_by_lm = get_usage() or {}
    usage = usage_by_lm.get(f"openai/{route}")
    if not isinstance(usage, Mapping):
        usage = next(
            (item for item in usage_by_lm.values() if isinstance(item, Mapping)),
            {},
        )
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )


def _string_list(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
    maximum: int = 12,
    item_limit: int = 800,
) -> list[str]:
    """Validate one bounded typed string-list prediction field."""
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise RuntimeError(f"DSPy {field_name} must contain {minimum}-{maximum} items.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > item_limit:
            raise RuntimeError(f"DSPy {field_name} contains an invalid item.")
        normalized.append(item.strip())
    return normalized


def _repository_paths(
    value: Any,
    *,
    field_name: str,
    worktree: Path,
    minimum: int = 0,
    maximum: int = 8,
) -> list[str]:
    """Validate bounded repository-relative paths that already exist."""
    items = _string_list(
        value,
        field_name=field_name,
        minimum=minimum,
        maximum=maximum,
        item_limit=240,
    )
    paths: list[str] = []
    for item in items:
        cleaned = item.strip("` ")
        candidate = PurePosixPath(cleaned)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise RuntimeError(f"DSPy {field_name} contains an unsafe path.")
        normalized = candidate.as_posix()
        if not (worktree / normalized).is_file():
            raise RuntimeError(
                f"DSPy {field_name} references a missing file: {normalized}"
            )
        if normalized not in paths:
            paths.append(normalized)
    if len(paths) < minimum:
        raise RuntimeError(f"DSPy {field_name} contains too few unique paths.")
    return paths


def _format_explorer_prediction(prediction: Any, *, worktree: Path) -> str:
    """Validate and render the typed explorer contract as manager-facing text."""
    findings = _string_list(
        _prediction_value(prediction, "findings"),
        field_name="explorer findings",
        minimum=1,
    )
    relevant_files = _repository_paths(
        _prediction_value(prediction, "relevant_files"),
        field_name="explorer relevant_files",
        worktree=worktree,
        minimum=1,
    )
    constraints = _string_list(
        _prediction_value(prediction, "constraints"),
        field_name="explorer constraints",
    )
    questions = _string_list(
        _prediction_value(prediction, "unresolved_questions"),
        field_name="explorer unresolved_questions",
    )
    sections = [
        "Read-only findings:\n" + "\n".join(f"- {item}" for item in findings),
        "Relevant files:\n" + "\n".join(f"- {item}" for item in relevant_files),
    ]
    if constraints:
        sections.append(
            "Constraints:\n" + "\n".join(f"- {item}" for item in constraints)
        )
    if questions:
        sections.append(
            "Unresolved questions:\n" + "\n".join(f"- {item}" for item in questions)
        )
    return "\n\n".join(sections)


def _format_planner_prediction(prediction: Any, *, worktree: Path) -> str:
    """Validate and render one typed task-plan step."""
    instruction = _prediction_value(prediction, "instruction")
    if (
        not isinstance(instruction, str)
        or not instruction.strip()
        or len(instruction) > 1200
    ):
        raise RuntimeError("DSPy planner instruction is invalid.")
    editable_files = _repository_paths(
        _prediction_value(prediction, "editable_files"),
        field_name="planner editable_files",
        worktree=worktree,
        minimum=1,
        maximum=2,
    )
    acceptance_criteria = _string_list(
        _prediction_value(prediction, "acceptance_criteria"),
        field_name="planner acceptance_criteria",
        minimum=1,
        maximum=6,
    )
    dependencies = _string_list(
        _prediction_value(prediction, "depends_on"),
        field_name="planner depends_on",
        maximum=6,
        item_limit=120,
    )
    sections = [
        f"Atomic instruction: {instruction.strip()}",
        "Editable files:\n" + "\n".join(f"- {item}" for item in editable_files),
        "Acceptance criteria:\n"
        + "\n".join(f"- {item}" for item in acceptance_criteria),
    ]
    if dependencies:
        sections.append(
            "Depends on:\n" + "\n".join(f"- {item}" for item in dependencies)
        )
    else:
        sections.append("Depends on: none")
    return "\n\n".join(sections)


def _implementation_report(tool_calls: list[dict[str, Any]]) -> tuple[str, bool]:
    """Summarize implementation from audited editor calls, not model prose."""
    editor_calls = [
        call for call in tool_calls if call.get("tool_name") == "apply_atomic_edit"
    ]
    if len(editor_calls) == 1 and editor_calls[0].get("status") == "success":
        output = str(editor_calls[0].get("output") or "Validated edit applied.")
        return f"Implementation succeeded: {output}", True
    if not editor_calls:
        return "Implementation failed: no apply_atomic_edit call was recorded.", False
    failed = [call for call in editor_calls if call.get("status") == "error"]
    if failed:
        output = str(failed[-1].get("output") or "Editor call failed.")
        return f"Implementation failed: {output}", False
    return (
        "Implementation failed: expected exactly one successful "
        f"apply_atomic_edit call, recorded {len(editor_calls)}.",
        False,
    )


@dataclass(frozen=True)
class AgentBundle:
    """The manager and its managed role agents."""

    manager: Any
    managed: tuple[Any, ...]


@dataclass
class ReadOnlyEvidenceAgent:
    """Gather bounded evidence, then invoke one typed DSPy role program."""

    name: str
    description: str
    activate_skill: Callable[[], Skill]
    context: ToolContext
    model_route: str
    lm_factory: Callable[[], Any]
    program_runner: Callable[..., Any]
    program_name: str
    state: StateStore | None = None
    run_id: str | None = None
    usage_budget: ModelUsageBudget | None = None
    _lm: Any | None = field(default=None, init=False, repr=False)

    def _language_model(self) -> Any:
        if self._lm is None:
            self._lm = self.lm_factory()
        return self._lm

    def __call__(
        self,
        task: str,
        additional_args: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        del additional_args
        step_id = (
            self.state.start_step(
                self.run_id,
                agent_role=self.name,
                summary=f"DSPy {self.name} evidence request",
            )
            if self.state is not None and self.run_id is not None
            else None
        )
        status = "completed"
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        started = time.perf_counter()
        metadata: dict[str, Any] = {
            "source": f"dspy-{self.name}",
            "program": self.program_name,
            "adapter": "JSONAdapter",
        }
        try:
            self.activate_skill()
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
            if self.usage_budget is not None:
                self.usage_budget.reserve_call()
            prediction = self.program_runner(
                lm=self._language_model(),
                task=authoritative_task,
                delegated_task=task,
                repository_evidence=evidence,
            )
            prompt_tokens, completion_tokens = _prediction_usage(
                prediction, self.model_route
            )
            if self.usage_budget is not None:
                self.usage_budget.record_usage(prompt_tokens, completion_tokens)
            if self.name == "explorer":
                response = _format_explorer_prediction(
                    prediction,
                    worktree=self.context.worktree.path,
                )
                response = _ground_evidence_response(
                    role=self.name,
                    response=response,
                    evidence=evidence,
                )
            elif self.name == "planner":
                response = _format_planner_prediction(
                    prediction,
                    worktree=self.context.worktree.path,
                )
            else:
                raise RuntimeError(f"Unsupported DSPy evidence role: {self.name}")
            metadata["status"] = "success"
            return response
        except Exception as exc:
            status = "failed"
            metadata.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            if self.state is not None and self.run_id is not None:
                self.state.add_model_metrics(
                    self.run_id,
                    route=self.model_route,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    metadata=metadata,
                )
            if self.state is not None and step_id is not None:
                self.state.complete_step(step_id, status=status)


def _implementer_files(task: str, context: ToolContext) -> list[str]:
    """Resolve one or two approved existing files from a delegated edit request."""
    named = list(dict.fromkeys(re.findall(r"[\w./-]+\.[A-Za-z0-9]+", task)))
    if not named and context.allowed_edit_paths is not None:
        named = sorted(context.allowed_edit_paths)
    paths = _repository_paths(
        named,
        field_name="implementer editable_files",
        worktree=context.worktree.path,
        minimum=1,
        maximum=2,
    )
    if context.allowed_edit_paths is not None:
        allowed = {
            context._normalized_path(item) for item in context.allowed_edit_paths
        }
        disallowed = {context._normalized_path(item) for item in paths} - allowed
        if disallowed:
            context.scope_violations.update(disallowed)
            joined = ", ".join(sorted(disallowed))
            raise RuntimeError(
                f"Edit request is outside the predeclared scope: {joined}"
            )
    return paths


@dataclass
class DSPyImplementerAgent:
    """Generate typed edits with DSPy, then delegate all writes to the editor."""

    name: str
    description: str
    activate_skill: Callable[[], Skill]
    context: ToolContext
    model_route: str
    lm_factory: Callable[[], Any]
    program_runner: Callable[..., Any]
    program_name: str
    state: StateStore | None = None
    run_id: str | None = None
    usage_budget: ModelUsageBudget | None = None
    _lm: Any | None = field(default=None, init=False, repr=False)

    def _language_model(self) -> Any:
        if self._lm is None:
            self._lm = self.lm_factory()
        return self._lm

    def __call__(
        self,
        task: str,
        additional_args: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        del additional_args
        step_id = (
            self.state.start_step(
                self.run_id,
                agent_role=self.name,
                summary="DSPy implementer request",
            )
            if self.state is not None and self.run_id is not None
            else None
        )
        status = "completed"
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        started = time.perf_counter()
        metadata: dict[str, Any] = {
            "source": "dspy-implementer",
            "program": self.program_name,
            "adapter": "JSONAdapter",
        }
        try:
            self.activate_skill()
            authoritative_task = self.context.task_file.read_text(encoding="utf-8")
            editable_files = _implementer_files(task, self.context)
            try:
                task_relative = str(
                    self.context.task_file.relative_to(self.context.worktree.path)
                )
            except ValueError:
                protected_files: set[str] = set()
            else:
                protected_files = {task_relative}
            contents = load_editable_files(
                self.context.worktree.path,
                editable_files,
                protected_files=protected_files,
            )
            file_contents = [
                f"--- {path} ---\n{contents[path]}" for path in editable_files
            ]
            if self.usage_budget is not None:
                self.usage_budget.reserve_call()
            prediction = self.program_runner(
                lm=self._language_model(),
                task=authoritative_task,
                instruction=task,
                editable_files=editable_files,
                file_contents=file_contents,
            )
            prompt_tokens, completion_tokens = _prediction_usage(
                prediction, self.model_route
            )
            if self.usage_budget is not None:
                self.usage_budget.record_usage(prompt_tokens, completion_tokens)
            result = self.context.apply_prepared_atomic_edits(
                task,
                ",".join(editable_files),
                _prediction_value(prediction, "edits"),
            )
            metadata["status"] = "success"
            return f"Implementation succeeded: {result}"
        except Exception as exc:
            status = "failed"
            metadata.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            if self.state is not None and self.run_id is not None:
                self.state.add_model_metrics(
                    self.run_id,
                    route=self.model_route,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    metadata=metadata,
                )
            if self.state is not None and step_id is not None:
                self.state.complete_step(step_id, status=status)


@dataclass
class ReadOnlyReviewAgent:
    """Run the fixed review gates without exposing a code executor."""

    name: str
    description: str
    context: ToolContext
    activate_skill: Callable[[], Skill] | None = None
    state: StateStore | None = None
    run_id: str | None = None

    def __call__(
        self,
        task: str,
        additional_args: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        del task, additional_args
        step_id = (
            self.state.start_step(
                self.run_id,
                agent_role=self.name,
                summary="Fixed read-only review gates",
            )
            if self.state is not None and self.run_id is not None
            else None
        )
        status = "completed"
        try:
            if self.activate_skill is not None:
                self.activate_skill()
            diff = self.context.inspect_diff()
            verification = self.context.run_verification()
            try:
                review = self.context.review_diff()
            except RuntimeError as exc:
                review = f"Review unavailable: {exc}"
            return f"{diff}\n\n{verification}\n\n{review}"
        except Exception:
            status = "failed"
            raise
        finally:
            if self.state is not None and step_id is not None:
                self.state.complete_step(step_id, status=status)


def _skill_binding(
    skills: SkillCatalog | Mapping[str, Skill], skill_name: str
) -> tuple[str, str, str, tuple[str, ...], int, Callable[[], Skill]]:
    """Return discovery fields, trusted limits, and a cached activator."""
    metadata = skills[skill_name]
    if isinstance(skills, SkillCatalog):
        config = runtime_skill_config(skill_name)
        activated: Skill | None = None

        def activate() -> Skill:
            nonlocal activated
            if activated is None:
                activated = activate_skill(skills, skill_name)
            return activated

        return (
            metadata.name,
            metadata.description,
            config.model,
            config.tools,
            config.max_steps,
            activate,
        )

    skill = metadata
    return (
        skill.name,
        skill.description,
        skill.model,
        skill.tools,
        skill.max_steps,
        lambda: skill,
    )


def _build_agent(
    *,
    role: str,
    skill_name: str,
    skills: SkillCatalog | Mapping[str, Skill],
    context: ToolContext,
    models: ModelRegistry,
    state: StateStore,
    run_id: str,
    usage_budget: ModelUsageBudget | None,
) -> Any:
    try:
        from smolagents import CodeAgent
    except ImportError as exc:
        raise RuntimeError(
            "smolagents is not installed. Run `make agent-install`."
        ) from exc

    (
        discovered_name,
        description,
        model_route,
        tool_names,
        max_steps,
        activate,
    ) = _skill_binding(skills, skill_name)

    class ManagedCodeAgent(CodeAgent):
        """Accept the small model's positional additional-arguments convention."""

        def __call__(
            self,
            task: str,
            additional_args: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            step_id = state.start_step(
                run_id,
                agent_role=role,
                summary="Managed code-agent request",
            )
            status = "completed"
            try:
                skill = activate()
                self.instructions = skill.instructions
                authoritative_task = context.task_file.read_text(encoding="utf-8")
                named_files = re.findall(r"[\w./-]+\.[A-Za-z0-9]+", task)
                first_file = named_files[0] if named_files else None
                if role == "implementer" and first_file:
                    first_action = (
                        "Your first action must call apply_atomic_edit exactly once, "
                        "using "
                        f"instruction={task!r} and editable_files={first_file!r}. "
                        "If that call fails, do not retry it with identical arguments; "
                        "finish with a concise failure report."
                    )
                else:
                    first_action = "Your first action must call run_verification()."
                constrained_task = (
                    "Every action must be valid Python inside <code> tags. Call your "
                    "allowed tools with Python keyword arguments before answering. "
                    "When finished, call the final_answer tool with exactly one "
                    "argument named answer. Put any requested report headings inside "
                    "that single answer string; never pass task_outcome_short, "
                    "task_outcome_detailed, or additional_context as arguments. Do "
                    "not narrate or claim success before the first tool call. "
                    f"{first_action}\n\n"
                    f"Authoritative task:\n{authoritative_task}\n\n"
                    f"Delegated task:\n{task}"
                )
                before = state.run_details(run_id) or {}
                before_ids = {call["id"] for call in before.get("tool_calls", [])}
                result = self.run(
                    constrained_task,
                    reset=True,
                    additional_args=additional_args,
                    **kwargs,
                )
                if role != "implementer":
                    return result
                after = state.run_details(run_id) or {}
                new_calls = [
                    call
                    for call in after.get("tool_calls", [])
                    if call["id"] not in before_ids and call.get("agent_role") == role
                ]
                report, succeeded = _implementation_report(new_calls)
                if not succeeded:
                    status = "failed"
                return report
            except Exception:
                status = "failed"
                raise
            finally:
                state.complete_step(step_id, status=status)

    role_context = ToolContext(
        root=context.root,
        worktree=context.worktree,
        run_id=context.run_id,
        state=context.state,
        task_file=context.task_file,
        agent_role=role,
        scope_violations=context.scope_violations,
        allowed_edit_paths=context.allowed_edit_paths,
    )
    state.register_agent(
        run_id,
        role=role,
        skill=discovered_name,
        model_route=model_route,
    )
    if role in {"explorer", "planner"}:
        from .dspy_programs.explorer import run_explorer_program
        from .dspy_programs.planner import run_planner_program

        program_runner = (
            run_explorer_program if role == "explorer" else run_planner_program
        )
        program_name = "ExplorerProgram" if role == "explorer" else "PlannerProgram"

        def lm_factory() -> Any:
            route = models.routes[model_route]
            return build_dspy_lm(
                model_route,
                api_base=models.api_base,
                api_key=models.api_key,
                max_tokens=route.max_tokens,
            )

        return ReadOnlyEvidenceAgent(
            name=role,
            description=description,
            activate_skill=activate,
            context=role_context,
            model_route=model_route,
            lm_factory=lm_factory,
            program_runner=program_runner,
            program_name=program_name,
            state=state,
            run_id=run_id,
            usage_budget=usage_budget,
        )
    if role == "implementer":
        from .dspy_programs.implementer import run_implementer_program

        def implementer_lm_factory() -> Any:
            route = models.routes[model_route]
            return build_dspy_lm(
                model_route,
                api_base=models.api_base,
                api_key=models.api_key,
                max_tokens=route.max_tokens,
            )

        return DSPyImplementerAgent(
            name=role,
            description=description,
            activate_skill=activate,
            context=role_context,
            model_route=model_route,
            lm_factory=implementer_lm_factory,
            program_runner=run_implementer_program,
            program_name="ImplementerProgram",
            state=state,
            run_id=run_id,
            usage_budget=usage_budget,
        )
    if role == "reviewer":
        return ReadOnlyReviewAgent(
            name=role,
            description=description,
            context=role_context,
            activate_skill=activate,
            state=state,
            run_id=run_id,
        )
    return ManagedCodeAgent(
        tools=build_smol_tools(role_context, tool_names),
        model=AuditedModel(
            models.build(model_route),
            route=model_route,
            state=state,
            run_id=run_id,
            usage_budget=usage_budget,
        ),
        instructions=None,
        max_steps=max_steps,
        name=role,
        description=description,
        provide_run_summary=False,
        add_base_tools=False,
        additional_authorized_imports=[],
        use_structured_outputs_internally=False,
        executor_kwargs={"timeout_seconds": 600},
    )


def build_agent_bundle(
    *,
    skills: SkillCatalog | Mapping[str, Skill],
    context: ToolContext,
    models: ModelRegistry,
    state: StateStore,
    run_id: str,
    manager_max_steps: int = 12,
    usage_budget: ModelUsageBudget | None = None,
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
            skill_name=skill_name,
            skills=skills,
            context=context,
            models=models,
            state=state,
            run_id=run_id,
            usage_budget=usage_budget,
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
        model=AuditedModel(
            models.build("local-plan"),
            route="local-plan",
            state=state,
            run_id=run_id,
            usage_budget=usage_budget,
        ),
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
