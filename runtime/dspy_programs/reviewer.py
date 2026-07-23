"""DSPy program for the fixed read-only reviewer boundary."""

from __future__ import annotations

from typing import Literal

import dspy

from runtime.prompt_activation import load_active_prompt_state

ReviewVerdict = Literal["pass", "fail", "needs_attention"]


class ReviewerSignature(dspy.Signature):
    """Review one code diff against its task and deterministic evidence.

    Use ``pass`` only when the requested behavior is satisfied, verification
    passed, and no material unrelated changes exist. Use ``fail`` for definite
    correctness or contract violations. Use ``needs_attention`` when the
    evidence requires additional independent judgement. Never edit files.
    """

    task: str = dspy.InputField(desc="The authoritative task text.")
    changed_files: list[str] = dspy.InputField(
        desc="Repository-relative paths present in the reviewed diff."
    )
    verification_passed: bool = dspy.InputField(
        desc="Whether deterministic make verify completed successfully."
    )
    verification_output: str = dspy.InputField(
        desc="The complete deterministic verification output."
    )
    diff: str = dspy.InputField(desc="The complete Git diff to review.")

    verdict: ReviewVerdict = dspy.OutputField(
        desc="One of pass, fail, or needs_attention."
    )
    summary: str = dspy.OutputField(
        desc=(
            "One concrete sentence naming the changed behavior and how the "
            "verification evidence supports the verdict."
        )
    )
    issues: list[str] = dspy.OutputField(
        desc="Definite issues, each naming a file path and concise reason."
    )
    unrelated_changes: list[str] = dspy.OutputField(
        desc="Material unrelated changes, each naming a file path and reason."
    )


class ReviewerProgram(dspy.Module):
    """Single-step structured reviewer suitable for the local 3B model."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.Predict(ReviewerSignature)

    def forward(
        self,
        *,
        task: str,
        changed_files: list[str],
        verification_passed: bool,
        verification_output: str,
        diff: str,
    ) -> dspy.Prediction:
        """Return the typed review prediction without exposing tools."""
        return self.predict(
            task=task,
            changed_files=changed_files,
            verification_passed=verification_passed,
            verification_output=verification_output,
            diff=diff,
        )


def run_reviewer_program(
    *,
    lm: object,
    task: str,
    changed_files: list[str],
    verification_passed: bool,
    verification_output: str,
    diff: str,
) -> dspy.Prediction:
    """Run the reviewer with JSON-typed decoding and per-call usage tracking."""
    program = ReviewerProgram()
    load_active_prompt_state(program, "reviewer")
    with dspy.context(
        lm=lm,
        adapter=dspy.JSONAdapter(),
        track_usage=True,
    ):
        return program(
            task=task,
            changed_files=changed_files,
            verification_passed=verification_passed,
            verification_output=verification_output,
            diff=diff,
        )
