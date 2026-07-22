"""DSPy program for the bounded atomic implementation boundary."""

from __future__ import annotations

import dspy
from pydantic import BaseModel, ConfigDict, Field


class AtomicEditSpec(BaseModel):
    """One exact replacement proposed for an approved existing file."""

    model_config = ConfigDict(extra="forbid")

    path: str
    old_text: str = Field(min_length=1)
    new_text: str


class ImplementerSignature(dspy.Signature):
    """Produce the smallest exact replacement batch for one atomic instruction.

    Use only the supplied approved files. ``old_text`` must be copied verbatim
    from the supplied file contents and must identify exactly one occurrence.
    Preserve all unrelated content. Do not describe, execute, or claim an edit;
    return only the typed replacement batch for the trusted native editor.
    """

    task: str = dspy.InputField(desc="The authoritative task text.")
    instruction: str = dspy.InputField(
        desc="One narrowly scoped implementation instruction."
    )
    editable_files: list[str] = dspy.InputField(
        desc="The approved existing repository-relative file paths."
    )
    file_contents: list[str] = dspy.InputField(
        desc="Complete bounded contents for each approved file, labelled by path."
    )

    edits: list[AtomicEditSpec] = dspy.OutputField(
        desc=(
            "One to eight exact search/replace edits. Paths must be approved; "
            "old_text must be a verbatim unique substring; preserve unrelated text."
        )
    )


class ImplementerProgram(dspy.Module):
    """Single-step typed edit generation suitable for the local 3B model."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.Predict(ImplementerSignature)

    def forward(
        self,
        *,
        task: str,
        instruction: str,
        editable_files: list[str],
        file_contents: list[str],
    ) -> dspy.Prediction:
        """Return a typed replacement batch without writing files."""
        return self.predict(
            task=task,
            instruction=instruction,
            editable_files=editable_files,
            file_contents=file_contents,
        )


def run_implementer_program(
    *,
    lm: object,
    task: str,
    instruction: str,
    editable_files: list[str],
    file_contents: list[str],
) -> dspy.Prediction:
    """Run the implementer with typed JSON decoding and usage tracking."""
    program = ImplementerProgram()
    with dspy.context(
        lm=lm,
        adapter=dspy.JSONAdapter(),
        track_usage=True,
    ):
        return program(
            task=task,
            instruction=instruction,
            editable_files=editable_files,
            file_contents=file_contents,
        )
