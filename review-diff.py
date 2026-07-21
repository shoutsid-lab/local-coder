#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
API_URL = "http://127.0.0.1:4000/v1/chat/completions"
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

    return result.returncode == 0, output


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


def call_reviewer(
    *,
    model: str,
    task: str,
    changed_files: list[str],
    diff: str,
    verification_passed: bool,
    verification_output: str,
) -> dict[str, Any]:
    prompt = f"""
Review this code change without editing any files.

Judge whether the change satisfies TASK.md, whether it introduces unrelated
changes, and whether the independent verification result supports acceptance.

Rules:

- Do not propose edits unless identifying a concrete issue.
- Do not claim verification passed when it failed.
- Treat protected tests and deterministic verification as authoritative.
- Return JSON only.
- Use verdict "pass" only when the task is satisfied, verification passed,
  and there are no material unrelated changes.
- Use "fail" for definite correctness or contract violations.
- Use "needs_attention" when human judgement is required.

TASK.md:

{task}

Changed files:

{json.dumps(changed_files, indent=2)}

Independent verification passed:

{verification_passed}

Independent verification output:

{verification_output}

Git diff:

{diff}
""".strip()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a read-only code reviewer. "
                    "Return a strict structured verdict and never edit files."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "response_format": {
            "type": "json_schema",
            "schema": review_schema(),
        },
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer local",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Reviewer API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ReviewError(f"Could not reach LiteLLM: {exc}") from exc

    try:
        content = result["choices"][0]["message"]["content"]
        review = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ReviewError(
            "The reviewer returned an invalid structured response."
        ) from exc

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
        default=ROOT / "TASK.md",
    )

    parser.add_argument(
        "--model",
        default="local-review",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
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

        review = call_reviewer(
            model=args.model,
            task=task,
            changed_files=changed_files,
            diff=diff,
            verification_passed=verification_passed,
            verification_output=verification_output,
        )

        args.output.write_text(
            json.dumps(review, indent=2) + "\n",
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
