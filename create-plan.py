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
DEFAULT_OUTPUT = ROOT / "PLAN.candidate.json"
DEFAULT_API_URL = "http://127.0.0.1:4000/v1/chat/completions"
RAW_OUTPUT_PATH = ROOT / "PLAN.candidate.raw.txt"

MAX_CONTEXT_FILE_BYTES = 32_000
MAX_STEPS = 8

PROTECTED_FILES = {
    "TASK.md",
    "PLAN.md",
    "PLAN.json",
    "PIPELINE.md",
    "CONVENTIONS.md",
    "test_pipeline_contract.py",
}


def is_protected_file(filename: str) -> bool:
    """Return whether a plan must never allow edits to this file."""
    path = Path(filename)
    return filename in PROTECTED_FILES or path.name.endswith("_contract.py")


class PlannerError(RuntimeError):
    """Raised when a candidate plan cannot be created safely."""

def run_verification_precheck() -> tuple[bool, str]:
    result = subprocess.run(
        ["make", "verify"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    combined_output = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    ).strip()

    return result.returncode == 0, combined_output

def write_already_satisfied_candidate(
    *,
    output_path: Path,
    base_branch: str,
) -> None:
    plan = {
        "name": "task-already-satisfied",
        "approved": False,
        "status": "already_satisfied",
        "base_branch": base_branch,
        "steps": [],
    }

    output_path.write_text(
        json.dumps(plan, indent=2) + "\n",
        encoding="utf-8",
    )

def command_output(command: list[str]) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def read_text_file(path: Path) -> str:
    if not path.is_file():
        raise PlannerError(f"Context file does not exist: {path}")

    data = path.read_bytes()

    if len(data) > MAX_CONTEXT_FILE_BYTES:
        raise PlannerError(
            f"Context file is too large: {path} "
            f"({len(data)} bytes; maximum {MAX_CONTEXT_FILE_BYTES})"
        )

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlannerError(f"Context file is not UTF-8 text: {path}") from exc


def repository_files() -> list[str]:
    output = command_output(["git", "ls-files"])
    return [line for line in output.splitlines() if line]


def current_branch() -> str:
    branch = command_output(["git", "branch", "--show-current"])

    if not branch:
        raise PlannerError("The repository is in detached HEAD state.")

    return branch


def build_prompt(
    task: str,
    context_files: list[Path],
    tracked_files: list[str],
    base_branch: str,
) -> str:
    context_sections: list[str] = []

    for path in context_files:
        relative = path.relative_to(ROOT)
        content = read_text_file(path)

        context_sections.append(
            f"--- {relative} ---\n{content}"
        )

    context_text = "\n\n".join(context_sections)
    tracked_text = "\n".join(f"- {name}" for name in tracked_files)

    return f"""
Create a machine-readable execution plan for a local coding agent.

Return JSON only. Do not use Markdown fences or explanatory prose.

The exact required structure is:

{{
  "name": "short-plan-name",
  "approved": false,
  "status": "planned",
  "base_branch": "{base_branch}",
  "steps": [
    {{
      "id": 1,
      "title": "Short step title",
      "instruction": "One precise atomic editing instruction.",
      "editable_files": ["existing/tracked/file.py"],
      "commit_message": "Imperative commit message"
    }}
  ]
}}

Rules:

- Set "approved" to false.
- Use between 1 and {MAX_STEPS} steps.
- Each step must describe one independently verifiable transformation.
- Each instruction must be explicit enough for a small 3B coding model.
- Prefer one editable file per step.
- Never use more than two editable files in one step.
- Use only existing tracked files listed below.
- Do not include protected tests as editable files.
- Treat every file whose name ends with `_contract.py` as protected.
- Do not edit TASK.md, PLAN.md, PLAN.json, PIPELINE.md,
  CONVENTIONS.md, or test_pipeline_contract.py.
- Do not include validation commands inside instructions.
- Preserve unrelated code.
- Order steps so every intermediate state can pass verification.
- Do not claim the plan is approved.
- Every step must edit at least one tracked, non-protected file.
- Do not create validation-only, testing-only, review-only, or commit-only steps.
- The executor runs verification and creates commits automatically.
- Do not include a final "run tests" or "verify changes" step.
- Each instruction must state one concrete source transformation.
- Do not repeat the same transformation in multiple steps.
- First determine whether the current repository already satisfies TASK.md.
- If the task is already satisfied, set "status" to "already_satisfied"
  and return an empty "steps" array.
- Do not invent refactors, cleanup, annotation, documentation, or test changes
  when the requested behaviour already exists.
- Otherwise set "status" to "planned" and provide atomic implementation steps.

Task:

{task}

Tracked repository files:

{tracked_text}

Repository context:

{context_text}
""".strip()



def build_plan_schema(
    tracked_files: list[str],
    base_branch: str,
) -> dict[str, Any]:
    editable_files = sorted(
        filename
        for filename in tracked_files
        if not is_protected_file(filename)
    )

    if not editable_files:
        raise PlannerError(
            "The repository contains no files eligible for editing."
        )

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "name",
            "approved",
            "status",
            "base_branch",
            "steps",
        ],
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "planned",
                    "already_satisfied",
                ],
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
            "approved": {
                "type": "boolean",
                "enum": [False],
            },
            "base_branch": {
                "type": "string",
                "enum": [base_branch],
            },
            "steps": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "title",
                        "instruction",
                        "editable_files",
                        "commit_message",
                    ],
                    "properties": {
                        "id": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_STEPS,
                        },
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                        },
                        "instruction": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1_500,
                        },
                        "editable_files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 2,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": editable_files,
                            },
                        },
                        "commit_message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                        },
                    },
                },
            },
        },
    }

def call_model(
    *,
    api_url: str,
    model: str,
    prompt: str,
    response_schema: dict[str, Any],
) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a non-editing software task planner. "
                    "Return valid JSON only."
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
            "schema": response_schema,
        },
    }

    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PlannerError(
            f"Planner API returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PlannerError(f"Could not reach planner API: {exc}") from exc

    try:
        return result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise PlannerError("Planner API returned an unexpected response.") from exc


def remove_code_fence(text: str) -> str:
    stripped = text.strip()

    if stripped.startswith("```"):
        lines = stripped.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        stripped = "\n".join(lines).strip()

    return stripped


def validate_candidate(
    plan: dict[str, Any],
    *,
    tracked_files: set[str],
    base_branch: str,
) -> None:
    if plan.get("approved") is not False:
        raise PlannerError(
            "Generated plan must set `approved` to false."
        )

    if plan.get("base_branch") != base_branch:
        raise PlannerError(
            "Generated plan changed the requested base branch."
        )

    name = plan.get("name")

    if not isinstance(name, str) or not name.strip():
        raise PlannerError("Generated plan has an invalid name.")



    status = plan.get("status")

    if status not in {"planned", "already_satisfied"}:
        raise PlannerError(
            "Generated plan has an invalid `status`."
        )

    steps = plan.get("steps")

    if not isinstance(steps, list):
        raise PlannerError(
            "Generated plan must contain a `steps` array."
        )

    if status == "already_satisfied":
        if steps:
            raise PlannerError(
                "An already-satisfied plan must have no steps."
            )
        return

    if not 1 <= len(steps) <= MAX_STEPS:
        raise PlannerError(
            f"A planned task must contain 1-{MAX_STEPS} steps."
        )

    for expected_id, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PlannerError(f"Step {expected_id} is not an object.")

        if step.get("id") != expected_id:
            raise PlannerError(
                "Step IDs must be consecutive and begin at 1."
            )

        for field in ("title", "instruction", "commit_message"):
            value = step.get(field)

            if not isinstance(value, str) or not value.strip():
                raise PlannerError(
                    f"Step {expected_id} has an invalid `{field}`."
                )

        files = step.get("editable_files")

        if not isinstance(files, list) or not 1 <= len(files) <= 2:
            raise PlannerError(
                f"Step {expected_id} must list one or two editable files."
            )

        for filename in files:
            if is_protected_file(filename):
                raise PlannerError(
                    f"Step {expected_id} attempts to edit protected file: "
                    f"{filename}"
                )

            if filename not in tracked_files:
                raise PlannerError(
                    f"Step {expected_id} references an untracked or "
                    f"missing file: {filename}"
                )


def write_candidate(
    *,
    output_path: Path,
    model_output: str,
    tracked_files: set[str],
    base_branch: str,
) -> None:
    cleaned = remove_code_fence(model_output)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PlannerError(
            "The planner did not return valid JSON.\n\n"
            f"Raw output:\n{model_output}"
        ) from exc

    if not isinstance(plan, dict):
        raise PlannerError("The planner returned a non-object JSON value.")

    validate_candidate(
        plan,
        tracked_files=tracked_files,
        base_branch=base_branch,
    )

    output_path.write_text(
        json.dumps(plan, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an unapproved atomic plan candidate."
    )

    parser.add_argument(
        "--task",
        type=Path,
        default=ROOT / "TASK.md",
        help="Task specification to plan.",
    )

    parser.add_argument(
        "--context",
        type=Path,
        nargs="*",
        default=[],
        help="Additional repository files to provide as planning context.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Candidate-plan output path.",
    )

    parser.add_argument(
        "--model",
        default="local-plan",
        help="Model alias exposed by LiteLLM.",
    )

    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="OpenAI-compatible chat-completions endpoint.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        task_path = args.task.resolve()
        output_path = args.output.resolve()

        precheck_passed, precheck_output = run_verification_precheck()

        context_paths = [
            (ROOT / "PIPELINE.md").resolve(),
            (ROOT / "CONVENTIONS.md").resolve(),
            task_path,
            *[path.resolve() for path in args.context],
        ]

        tracked = repository_files()
        branch = current_branch()
        task = read_text_file(task_path)

        if precheck_passed:
            write_already_satisfied_candidate(
                output_path=output_path,
                base_branch=branch,
            )

            print(f"Candidate plan written to: {output_path}")
            print("Status: already_satisfied")
            print("The independent verification pipeline already passes.")
            print("The local model was not called.")
            return 0

        prompt = build_prompt(
            task,
            context_paths,
            tracked,
            branch,
        )

        response_schema = build_plan_schema(
            tracked,
            branch,
        )

        model_output = call_model(
            api_url=args.api_url,
            model=args.model,
            prompt=prompt,
            response_schema=response_schema,
        )

        RAW_OUTPUT_PATH.write_text(
            model_output + "\n",
            encoding="utf-8",
        )

        write_candidate(
            output_path=output_path,
            model_output=model_output,
            tracked_files=set(tracked),
            base_branch=branch,
        )

        print(f"Candidate plan written to: {output_path}")
        print("The candidate remains unapproved.")
        print()
        print("Review it with:")
        print(f"  cat {output_path}")
        print(
            f"  ./run-plan.py --plan {output_path} --dry-run"
        )

    except PlannerError as exc:
        print(f"Planner error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"Command failed with status {exc.returncode}.",
            file=sys.stderr,
        )
        return exc.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
