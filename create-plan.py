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
DEFAULT_API_URL = "http://127.0.0.1:8080/v1/chat/completions"

MAX_CONTEXT_FILE_BYTES = 32_000
MAX_STEPS = 8


class PlannerError(RuntimeError):
    """Raised when a candidate plan cannot be created safely."""


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
- Do not edit TASK.md, PLAN.md, PLAN.json, PIPELINE.md,
  CONVENTIONS.md, or test_pipeline_contract.py.
- Do not include validation commands inside instructions.
- Preserve unrelated code.
- Order steps so every intermediate state can pass verification.
- Do not claim the plan is approved.

Task:

{task}

Tracked repository files:

{tracked_text}

Repository context:

{context_text}
""".strip()


def call_model(
    *,
    api_url: str,
    model: str,
    prompt: str,
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

    steps = plan.get("steps")

    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PlannerError(
            f"Generated plan must contain 1-{MAX_STEPS} steps."
        )

    protected = {
        "TASK.md",
        "PLAN.md",
        "PLAN.json",
        "PIPELINE.md",
        "CONVENTIONS.md",
        "test_pipeline_contract.py",
    }

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
            if filename in protected:
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
        default="local-coder",
        help="Model alias exposed by llama-server.",
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

        context_paths = [
            (ROOT / "PIPELINE.md").resolve(),
            (ROOT / "CONVENTIONS.md").resolve(),
            task_path,
            *[path.resolve() for path in args.context],
        ]

        tracked = repository_files()
        branch = current_branch()
        task = read_text_file(task_path)

        prompt = build_prompt(
            task,
            context_paths,
            tracked,
            branch,
        )

        model_output = call_model(
            api_url=args.api_url,
            model=args.model,
            prompt=prompt,
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
