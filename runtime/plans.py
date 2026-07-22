"""Trusted validation for human-authored atomic task plans."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .editor import EditorError, load_editable_files

PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 64_000
MAX_PLAN_STEPS = 12
MAX_STEP_FILES = 2
MAX_ACCEPTANCE_CRITERIA = 8
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PlanError(ValueError):
    """Raised when a task plan violates the trusted plan contract."""


@dataclass(frozen=True)
class PlanStep:
    """One manually selected atomic implementation step."""

    id: str
    instruction: str
    editable_files: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class TaskPlan:
    """A validated, canonical human-authored task plan."""

    schema_version: int
    plan_id: str
    objective: str
    steps: tuple[PlanStep, ...]
    plan_hash: str

    def step(self, step_id: str) -> PlanStep:
        """Return one plan step or fail closed for an unknown identifier."""
        for step in self.steps:
            if step.id == step_id:
                return step
        raise PlanError(f"Unknown plan step: {step_id}")


def _exact_keys(payload: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise PlanError(f"{label} has invalid fields: {', '.join(details)}")


def _text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlanError(f"{label} must be text.")
    normalized = value.strip()
    if not normalized:
        raise PlanError(f"{label} must not be empty.")
    if len(normalized) > maximum:
        raise PlanError(f"{label} exceeds {maximum} characters.")
    return normalized


def _identifier(value: Any, label: str) -> str:
    identifier = _text(value, label, maximum=64)
    if not _IDENTIFIER.fullmatch(identifier):
        raise PlanError(
            f"{label} must use letters, numbers, dots, underscores, or hyphens."
        )
    return identifier


def _string_list(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
    item_maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PlanError(f"{label} must contain between {minimum} and {maximum} items.")
    items = tuple(
        _text(item, f"{label} item", maximum=item_maximum) for item in value
    )
    if len(set(items)) != len(items):
        raise PlanError(f"{label} items must be unique.")
    return items


def _plan_relative_path(plan_path: Path, repository: Path) -> str | None:
    try:
        return plan_path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return None


def _canonical_payload(
    *,
    plan_id: str,
    objective: str,
    steps: tuple[PlanStep, ...],
) -> dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "objective": objective,
        "steps": [asdict(step) for step in steps],
    }


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_task_plan(
    payload: Any,
    *,
    repository: Path,
    plan_path: Path | None = None,
) -> TaskPlan:
    """Validate a strict plan payload against the current repository contents."""
    if not isinstance(payload, dict):
        raise PlanError("Task plan must be a JSON object.")
    _exact_keys(
        payload,
        {"schema_version", "plan_id", "objective", "steps"},
        "Task plan",
    )
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != PLAN_SCHEMA_VERSION
    ):
        raise PlanError(f"Unsupported task-plan schema version: {schema_version}")

    plan_id = _identifier(payload["plan_id"], "plan_id")
    objective = _text(payload["objective"], "objective", maximum=2_000)
    raw_steps = payload["steps"]
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
        raise PlanError(
            f"steps must contain between 1 and {MAX_PLAN_STEPS} entries."
        )

    protected_files: set[str] = set()
    if plan_path is not None:
        relative_plan = _plan_relative_path(plan_path, repository)
        if relative_plan is not None:
            protected_files.add(relative_plan)

    seen: set[str] = set()
    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise PlanError(f"Step {index} must be a JSON object.")
        _exact_keys(
            raw_step,
            {
                "id",
                "instruction",
                "editable_files",
                "acceptance_criteria",
                "depends_on",
            },
            f"Step {index}",
        )
        step_id = _identifier(raw_step["id"], f"Step {index} id")
        if step_id in seen:
            raise PlanError(f"Duplicate step id: {step_id}")
        instruction = _text(
            raw_step["instruction"],
            f"Step {step_id} instruction",
            maximum=2_000,
        )
        raw_editable_files = _string_list(
            raw_step["editable_files"],
            f"Step {step_id} editable_files",
            minimum=1,
            maximum=MAX_STEP_FILES,
            item_maximum=240,
        )
        editable_files = tuple(
            Path(relative).as_posix() for relative in raw_editable_files
        )
        if len(set(editable_files)) != len(editable_files):
            raise PlanError(
                f"Step {step_id} editable_files contain duplicate path aliases."
            )
        acceptance_criteria = _string_list(
            raw_step["acceptance_criteria"],
            f"Step {step_id} acceptance_criteria",
            minimum=1,
            maximum=MAX_ACCEPTANCE_CRITERIA,
            item_maximum=500,
        )
        depends_on = _string_list(
            raw_step["depends_on"],
            f"Step {step_id} depends_on",
            minimum=0,
            maximum=MAX_PLAN_STEPS - 1,
            item_maximum=64,
        )
        invalid_dependencies = set(depends_on) - seen
        if invalid_dependencies:
            dependencies = ", ".join(sorted(invalid_dependencies))
            raise PlanError(
                f"Step {step_id} depends on unknown or later steps: {dependencies}"
            )
        try:
            load_editable_files(
                repository,
                list(editable_files),
                protected_files=protected_files,
            )
        except EditorError as exc:
            raise PlanError(f"Step {step_id}: {exc}") from exc

        steps.append(
            PlanStep(
                id=step_id,
                instruction=instruction,
                editable_files=editable_files,
                acceptance_criteria=acceptance_criteria,
                depends_on=depends_on,
            )
        )
        seen.add(step_id)

    frozen_steps = tuple(steps)
    canonical = _canonical_payload(
        plan_id=plan_id,
        objective=objective,
        steps=frozen_steps,
    )
    return TaskPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        objective=objective,
        steps=frozen_steps,
        plan_hash=_hash_payload(canonical),
    )


def load_task_plan(path: Path, *, repository: Path) -> TaskPlan:
    """Load and validate one bounded task-plan JSON file."""
    if path.is_symlink():
        raise PlanError("Task plan must not be a symbolic link.")
    resolved = path.resolve()
    if not resolved.is_file():
        raise PlanError(f"Task plan does not exist: {path}")
    data = resolved.read_bytes()
    if len(data) > MAX_PLAN_BYTES:
        raise PlanError(f"Task plan exceeds {MAX_PLAN_BYTES} bytes.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PlanError("Task plan must be UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"Task plan contains invalid JSON: {exc.msg}") from exc
    return parse_task_plan(payload, repository=repository, plan_path=resolved)


def plan_summary(plan: TaskPlan) -> dict[str, Any]:
    """Return the stable, inspection-oriented plan summary."""
    return {
        "schema_version": plan.schema_version,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "objective": plan.objective,
        "step_count": len(plan.steps),
        "steps": [
            {
                "id": step.id,
                "instruction": step.instruction,
                "editable_files": list(step.editable_files),
                "depends_on": list(step.depends_on),
                "acceptance_criteria": list(step.acceptance_criteria),
            }
            for step in plan.steps
        ],
    }


def render_step_task(plan: TaskPlan, step: PlanStep) -> str:
    """Render one plan step as the authoritative atomic task text."""
    editable = "\n".join(f"- {path}" for path in step.editable_files)
    criteria = "\n".join(f"- {criterion}" for criterion in step.acceptance_criteria)
    dependencies = (
        "\n".join(f"- {dependency}" for dependency in step.depends_on)
        if step.depends_on
        else "- none"
    )
    return f"""# Approved Atomic Plan Step

Plan ID: {plan.plan_id}
Plan hash: {plan.plan_hash}
Step ID: {step.id}

Objective:
{plan.objective}

Instruction:
{step.instruction}

Editable files:
{editable}

Acceptance criteria:
{criteria}

Declared dependencies:
{dependencies}

Boundaries:
- Modify only the editable files listed above.
- Use exact validated replacements; do not create, rename, or delete files.
- Do not stage, commit, merge, push, or weaken verification.
- Stop with needs_attention rather than expanding scope.
""".strip()
