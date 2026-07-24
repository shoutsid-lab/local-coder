#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from runtime.verification_evidence import summarize_verification_output

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "REVIEW.json"


class ReviewError(RuntimeError):
    """Raised when the review cannot be completed safely."""


def run(
    command: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def read_file(path: Path) -> str:
    if not path.is_file():
        raise ReviewError(f"Required file does not exist: {path}")

    return path.read_text(encoding="utf-8")


def untracked_diff() -> tuple[str, list[str]]:
    names_result = run(
        ["git", "ls-files", "--others", "--exclude-standard"], check=False
    )
    if names_result.returncode != 0:
        raise ReviewError(
            names_result.stderr.strip() or "Could not list untracked files."
        )

    sections: list[str] = []
    names: list[str] = []
    for relative in names_result.stdout.splitlines():
        path = ROOT / relative
        if not path.is_file():
            continue
        result = run(
            ["git", "diff", "--no-index", "--", "/dev/null", relative],
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise ReviewError(result.stderr.strip() or f"Could not diff {relative}.")
        if result.stdout.strip():
            names.append(relative)
            sections.append(result.stdout.strip())

    return "\n\n".join(sections), names


def collect_diff(*, cached: bool, base: str | None) -> tuple[str, list[str]]:
    if base:
        diff_command = ["git", "diff", "--no-ext-diff", f"{base}...HEAD"]
        names_command = ["git", "diff", "--name-only", f"{base}...HEAD"]
    elif cached:
        diff_command = ["git", "diff", "--cached", "--no-ext-diff"]
        names_command = ["git", "diff", "--cached", "--name-only"]
    else:
        diff_command = ["git", "diff", "--no-ext-diff"]
        names_command = ["git", "diff", "--name-only"]

    diff = run(diff_command).stdout.strip()
    names = [line for line in run(names_command).stdout.splitlines() if line.strip()]

    if not base and not cached:
        extra_diff, extra_names = untracked_diff()
        if extra_diff:
            diff = "\n\n".join(part for part in (diff, extra_diff) if part)
            names.extend(name for name in extra_names if name not in names)

    if not diff:
        raise ReviewError("There are no changes to review.")

    return diff, names


def run_verification() -> tuple[bool, str]:
    result = run(["make", "verify"], check=False)

    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )

    passed = result.returncode == 0
    evidence = summarize_verification_output(output, passed=passed)
    return passed, evidence.model_output()


def review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "summary",
            "issues",
            "unrelated_changes",
        ],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "fail", "needs_attention"],
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            },
            "issues": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
            },
            "unrelated_changes": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
            },
        },
    }


def verdict_only_context(
    changed_files: list[str],
    verification_passed: bool,
) -> str:
    """Describe deterministic evidence when the model omits its explanation."""
    if changed_files:
        displayed = [name[:120] for name in changed_files[:3]]
        files = ", ".join(displayed)
        remaining = len(changed_files) - len(displayed)
        if remaining:
            files += f", and {remaining} other changed file(s)"
    else:
        files = "the reviewed diff"
    verification = "passed" if verification_passed else "failed"
    return (
        f"Deterministic verification {verification} for {files}. "
        "The model supplied no additional explanation."
    )


def validate_review_object(
    candidate: dict[str, Any],
    *,
    verdict_only_evidence: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize one structured reviewer result."""
    required = {"verdict", "summary", "issues", "unrelated_changes"}
    if set(candidate) == {"verdict"} and candidate["verdict"] in {
        "pass",
        "fail",
        "needs_attention",
    }:
        verdict = candidate["verdict"]
        detail = verdict_only_evidence or "No explanatory details were supplied."
        return {
            "verdict": verdict,
            "summary": f"The local reviewer returned verdict {verdict!r}. {detail}",
            "issues": [],
            "unrelated_changes": [],
        }
    if set(candidate) != required:
        raise ReviewError("The reviewer returned an invalid structured response.")
    if candidate["verdict"] not in {"pass", "fail", "needs_attention"}:
        raise ReviewError("The reviewer returned an invalid structured response.")
    summary = candidate["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000:
        raise ReviewError("The reviewer returned an invalid structured response.")
    details = (candidate["issues"], candidate["unrelated_changes"])
    if any(
        not isinstance(items, list)
        or len(items) > 20
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500
            for item in items
        )
        for items in details
    ):
        raise ReviewError("The reviewer returned an invalid structured response.")
    return {
        "verdict": candidate["verdict"],
        "summary": summary.strip(),
        "issues": [item.strip() for item in candidate["issues"]],
        "unrelated_changes": [item.strip() for item in candidate["unrelated_changes"]],
    }


def parse_review_content(
    content: str,
    *,
    verdict_only_evidence: str | None = None,
) -> dict[str, Any]:
    """Extract and validate one reviewer JSON object from model text."""
    decoder = json.JSONDecoder()
    for offset, character in enumerate(content):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[offset:])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        try:
            return validate_review_object(
                candidate,
                verdict_only_evidence=verdict_only_evidence,
            )
        except ReviewError:
            continue
    raise ReviewError("The reviewer returned an invalid structured response.")


def _prediction_value(prediction: Any, name: str) -> Any:
    """Read one DSPy prediction field from object or mapping output."""
    if isinstance(prediction, dict):
        return prediction[name]
    return getattr(prediction, name)


def _prediction_usage(prediction: Any, model: str) -> dict[str, Any]:
    """Return token usage attached by DSPy's per-call tracker."""
    get_usage = getattr(prediction, "get_lm_usage", None)
    if not callable(get_usage):
        return {}
    usage_by_lm = get_usage() or {}
    preferred = usage_by_lm.get(f"openai/{model}")
    if isinstance(preferred, dict):
        return preferred
    for usage in usage_by_lm.values():
        if isinstance(usage, dict):
            return usage
    return {}


def _run_dspy_reviewer(
    *,
    model: str,
    task: str,
    changed_files: list[str],
    diff: str,
    verification_passed: bool,
    verification_output: str,
) -> Any:
    """Construct and invoke the DSPy reviewer behind the fixed adapter."""
    from runtime.dspy_lm import build_dspy_lm_with_profile
    from runtime.dspy_programs.reviewer import run_reviewer_program
    from runtime.role_profiles import role_generation_profile, role_route
    from runtime.route_profiles import get_route_profile

    profile = (
        role_generation_profile("reviewer")
        if model == role_route("reviewer")
        else get_route_profile(model)
    )
    lm = build_dspy_lm_with_profile(model, profile)
    return run_reviewer_program(
        lm=lm,
        task=task,
        changed_files=changed_files,
        verification_passed=verification_passed,
        verification_output=verification_output,
        diff=diff,
    )


def call_reviewer(
    *,
    model: str,
    task: str,
    changed_files: list[str],
    diff: str,
    verification_passed: bool,
    verification_output: str,
    metrics_callback: Callable[..., None] | None = None,
    reviewer_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the typed DSPy reviewer and preserve the legacy verdict contract."""
    runner = reviewer_runner or _run_dspy_reviewer
    started = time.perf_counter()
    try:
        prediction = runner(
            model=model,
            task=task,
            changed_files=changed_files,
            diff=diff,
            verification_passed=verification_passed,
            verification_output=verification_output,
        )
        review = validate_review_object(
            {
                "verdict": _prediction_value(prediction, "verdict"),
                "summary": _prediction_value(prediction, "summary"),
                "issues": _prediction_value(prediction, "issues"),
                "unrelated_changes": _prediction_value(prediction, "unrelated_changes"),
            },
            verdict_only_evidence=verdict_only_context(
                changed_files, verification_passed
            ),
        )
        if review["verdict"] == "pass" and not verification_passed:
            raise ReviewError(
                "The reviewer cannot pass a change when verification failed."
            )
        if review["verdict"] == "pass" and (
            review["issues"] or review["unrelated_changes"]
        ):
            raise ReviewError("The reviewer cannot pass a change with reported issues.")
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError(f"DSPy reviewer failed: {exc}") from exc

    if metrics_callback is not None:
        usage = _prediction_usage(prediction, model)
        metrics_callback(
            route=model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            duration_ms=(time.perf_counter() - started) * 1000,
            metadata={
                "status": "success",
                "source": "dspy-reviewer",
                "program": "ReviewerProgram",
                "adapter": "JSONAdapter",
            },
        )

    return review


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review a Git diff using the read-only local-review role."
    )

    source = parser.add_mutually_exclusive_group()

    source.add_argument(
        "--cached",
        action="store_true",
        help="Review staged changes.",
    )

    source.add_argument(
        "--base",
        help="Review committed changes relative to a base branch.",
    )

    parser.add_argument(
        "--task",
        type=Path,
        required=True,
        help="Authoritative task file for the change under review.",
    )

    from runtime.role_profiles import role_route

    parser.add_argument("--model", default=role_route("reviewer"))

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )

    parser.add_argument(
        "--metrics-output",
        type=Path,
        help="Optional private sidecar for model usage audit data.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        task = read_file(args.task.resolve())
        diff, changed_files = collect_diff(
            cached=args.cached,
            base=args.base,
        )

        verification_passed, verification_output = run_verification()

        metrics: dict[str, Any] = {}
        review = call_reviewer(
            model=args.model,
            task=task,
            changed_files=changed_files,
            diff=diff,
            verification_passed=verification_passed,
            verification_output=verification_output,
            metrics_callback=lambda **values: metrics.update(values),
        )

        args.output.write_text(
            json.dumps(review, indent=2) + "\n",
            encoding="utf-8",
        )
        if args.metrics_output is not None:
            args.metrics_output.write_text(
                json.dumps(metrics, indent=2) + "\n",
                encoding="utf-8",
            )

        print(json.dumps(review, indent=2))
        print()
        print(f"Review written to: {args.output}")

        return 0 if review["verdict"] == "pass" else 1

    except ReviewError as exc:
        print(f"Review error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"Command failed with status {exc.returncode}.",
            file=sys.stderr,
        )
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
