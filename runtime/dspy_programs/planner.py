"""DSPy program for the read-only atomic planner boundary."""

from __future__ import annotations

import dspy

from runtime.prompt_activation import load_active_prompt_state
from runtime.role_profiles import apply_qualified_instructions


class PlannerSignature(dspy.Signature):
    """Convert supplied repository evidence into one atomic task-plan step.

    Produce an implementation-ready instruction without editing files or
    emitting executable tool calls. Keep the editable scope to one or two
    repository-relative files and preserve unrelated behavior.
    """

    task: str = dspy.InputField(desc="The authoritative task text.")
    delegated_task: str = dspy.InputField(
        desc="The manager's focused planning request."
    )
    repository_evidence: list[str] = dspy.InputField(
        desc="Focused repository excerpts or a bounded file listing."
    )

    instruction: str = dspy.OutputField(
        desc="One explicit atomic transformation suitable for the native editor."
    )
    editable_files: list[str] = dspy.OutputField(
        desc="One or two existing repository-relative files that may be edited."
    )
    acceptance_criteria: list[str] = dspy.OutputField(
        desc="One to six observable checks proving the instruction is complete."
    )
    depends_on: list[str] = dspy.OutputField(
        desc="Earlier step identifiers; use an empty list for a standalone step."
    )


class PlannerProgram(dspy.Module):
    """Reason over bounded evidence and return a typed atomic task-plan step."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.ChainOfThought(PlannerSignature)

    def forward(
        self,
        *,
        task: str,
        delegated_task: str,
        repository_evidence: list[str],
    ) -> dspy.Prediction:
        """Return the typed atomic plan prediction."""
        return self.predict(
            task=task,
            delegated_task=delegated_task,
            repository_evidence=repository_evidence,
        )


def run_planner_program(
    *,
    lm: object,
    task: str,
    delegated_task: str,
    repository_evidence: list[str],
) -> dspy.Prediction:
    """Run the planner with typed JSON decoding and per-call usage tracking."""
    program = PlannerProgram()
    if not apply_qualified_instructions(program, "planner"):
        load_active_prompt_state(program, "planner")
    with dspy.context(
        lm=lm,
        adapter=dspy.JSONAdapter(),
        track_usage=True,
    ):
        return program(
            task=task,
            delegated_task=delegated_task,
            repository_evidence=repository_evidence,
        )
