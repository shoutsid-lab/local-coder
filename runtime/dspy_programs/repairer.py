"""DSPy program for the bounded deterministic repair boundary."""

from __future__ import annotations

import dspy

from runtime.prompt_activation import load_active_prompt_state

from .implementer import AtomicEditSpec


class RepairerSignature(dspy.Signature):
    """Repair one deterministic verification failure with one exact edit batch.

    Diagnose only the supplied latest failure. Use only the approved existing
    files and current diff. ``old_text`` must be copied verbatim from the
    supplied file contents and identify exactly one occurrence. Never weaken
    tests, verification commands, or acceptance criteria. Return one bounded
    repair batch for the trusted native editor; do not claim it was applied.
    """

    task: str = dspy.InputField(desc="The authoritative task text.")
    delegated_task: str = dspy.InputField(
        desc="The manager's focused request to repair one verification failure."
    )
    verification_output: str = dspy.InputField(
        desc="The complete latest deterministic verification failure output."
    )
    diff: str = dspy.InputField(desc="The complete current uncommitted Git diff.")
    editable_files: list[str] = dspy.InputField(
        desc="One or two approved existing repository-relative file paths."
    )
    file_contents: list[str] = dspy.InputField(
        desc="Complete bounded contents for each approved file, labelled by path."
    )

    diagnosis: str = dspy.OutputField(
        desc="One concise root cause supported by the supplied failure evidence."
    )
    edits: list[AtomicEditSpec] = dspy.OutputField(
        desc=(
            "One to eight exact search/replace edits for the single diagnosed "
            "failure. Paths must be approved; preserve unrelated behavior."
        )
    )


class RepairerProgram(dspy.Module):
    """Single-step typed repair generation suitable for the local 3B model."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.Predict(RepairerSignature)

    def forward(
        self,
        *,
        task: str,
        delegated_task: str,
        verification_output: str,
        diff: str,
        editable_files: list[str],
        file_contents: list[str],
    ) -> dspy.Prediction:
        """Return one typed repair batch without writing files."""
        return self.predict(
            task=task,
            delegated_task=delegated_task,
            verification_output=verification_output,
            diff=diff,
            editable_files=editable_files,
            file_contents=file_contents,
        )


def run_repairer_program(
    *,
    lm: object,
    task: str,
    delegated_task: str,
    verification_output: str,
    diff: str,
    editable_files: list[str],
    file_contents: list[str],
) -> dspy.Prediction:
    """Run the repairer with typed JSON decoding and usage tracking."""
    program = RepairerProgram()
    load_active_prompt_state(program, "repairer")
    with dspy.context(
        lm=lm,
        adapter=dspy.JSONAdapter(),
        track_usage=True,
    ):
        return program(
            task=task,
            delegated_task=delegated_task,
            verification_output=verification_output,
            diff=diff,
            editable_files=editable_files,
            file_contents=file_contents,
        )
