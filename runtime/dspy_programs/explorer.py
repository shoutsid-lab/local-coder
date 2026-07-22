"""DSPy program for the read-only repository explorer boundary."""

from __future__ import annotations

import dspy


class ExplorerSignature(dspy.Signature):
    """Turn supplied repository evidence into grounded read-only findings.

    Report only facts supported by the supplied evidence. Identify the smallest
    relevant file surface, constraints, and unresolved questions. Never claim
    that an edit was applied, completed, or successful, and never prescribe an
    implementation plan.
    """

    task: str = dspy.InputField(desc="The authoritative task text.")
    delegated_task: str = dspy.InputField(
        desc="The manager's focused read-only exploration request."
    )
    repository_evidence: list[str] = dspy.InputField(
        desc="Focused repository excerpts or a bounded file listing."
    )

    findings: list[str] = dspy.OutputField(
        desc="Observed facts, each tied to a supplied file, symbol, or excerpt."
    )
    relevant_files: list[str] = dspy.OutputField(
        desc="Repository-relative paths directly relevant to the task."
    )
    constraints: list[str] = dspy.OutputField(
        desc="Tests, conventions, protected surfaces, or scope constraints."
    )
    unresolved_questions: list[str] = dspy.OutputField(
        desc="Questions the supplied evidence cannot answer; use an empty list if none."
    )


class ExplorerProgram(dspy.Module):
    """Reason over bounded evidence without exposing repository tools."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.ChainOfThought(ExplorerSignature)

    def forward(
        self,
        *,
        task: str,
        delegated_task: str,
        repository_evidence: list[str],
    ) -> dspy.Prediction:
        """Return typed read-only findings."""
        return self.predict(
            task=task,
            delegated_task=delegated_task,
            repository_evidence=repository_evidence,
        )


def run_explorer_program(
    *,
    lm: object,
    task: str,
    delegated_task: str,
    repository_evidence: list[str],
) -> dspy.Prediction:
    """Run the explorer with typed JSON decoding and per-call usage tracking."""
    program = ExplorerProgram()
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
