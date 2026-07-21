#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PLAN_PATH = ROOT / "PLAN.json"
WORKTREE_ROOT = ROOT.parent / f"{ROOT.name}-worktrees"

PROTECTED_FILES = {
    "AGENTS.md",
    "TASK.md",
    "PLAN.md",
    "PLAN.json",
    "PIPELINE.md",
    "CONVENTIONS.md",
    "Makefile",
    "create-plan.py",
    "litellm-config.yaml",
    "local-coder.py",
    "review-diff.py",
    "run-editor.py",
    "run-plan.py",
    "test_pipeline_contract.py",
}


def is_protected_file(filename: str) -> bool:
    """Return whether a plan must never allow edits to this file."""
    path = Path(filename)
    return filename in PROTECTED_FILES or path.name.endswith("_contract.py")


class PipelineError(RuntimeError):
    """Raised when the execution pipeline cannot safely continue."""


def run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}")

    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


def output(command: list[str], *, cwd: Path = ROOT) -> str:
    result = run(command, cwd=cwd, capture=True)
    return result.stdout.strip()


def require_clean_repository() -> None:
    status = output(["git", "status", "--porcelain"])

    if status:
        raise PipelineError(
            "The base repository has uncommitted changes.\n"
            "Commit, stash, or restore them before running a plan."
        )


def load_plan(
    path: Path,
    *,
    require_approval: bool = False,
) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"Plan file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON in {path}: {exc}") from exc

    validate_plan(plan, require_approval=require_approval)
    return plan


def validate_plan(
    plan: dict[str, Any],
    *,
    require_approval: bool = False,
) -> None:
    if require_approval and plan.get("approved") is not True:
        raise PipelineError(
            "The plan has not been approved. Set `approved` to true "
            "only after reviewing every step."
        )

    if not isinstance(plan.get("approved"), bool):
        raise PipelineError("The plan must contain a boolean `approved` field.")

    if not isinstance(plan.get("name"), str) or not plan["name"].strip():
        raise PipelineError("The plan must contain a non-empty `name`.")

    if not isinstance(plan.get("base_branch"), str):
        raise PipelineError("The plan must contain `base_branch`.")

    status = plan.get("status")

    if status not in {"planned", "already_satisfied"}:
        raise PipelineError("The plan must contain a valid `status`.")

    steps = plan.get("steps")

    if not isinstance(steps, list):
        raise PipelineError("The plan must contain a `steps` array.")

    if status == "already_satisfied":
        if steps:
            raise PipelineError("An already-satisfied plan must have no steps.")

        if plan.get("approved") is not False:
            raise PipelineError("An already-satisfied plan must remain unapproved.")

        return

    if not steps:
        raise PipelineError("A planned task must contain at least one step.")

    if require_approval and plan.get("approved") is not True:
        raise PipelineError("The plan has not been approved.")

    seen_ids: set[int] = set()

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PipelineError(f"Step {index} must be an object.")

        step_id = step.get("id")

        if not isinstance(step_id, int):
            raise PipelineError(f"Step {index} must have an integer `id`.")

        if step_id in seen_ids:
            raise PipelineError(f"Duplicate step id: {step_id}")

        seen_ids.add(step_id)

        for field in ("title", "instruction", "commit_message"):
            value = step.get(field)

            if not isinstance(value, str) or not value.strip():
                raise PipelineError(
                    f"Step {step_id} must contain a non-empty `{field}`."
                )

        files = step.get("editable_files")

        if not isinstance(files, list) or not files:
            raise PipelineError(
                f"Step {step_id} must contain at least one editable file."
            )

        for filename in files:
            if not isinstance(filename, str) or not filename.strip():
                raise PipelineError(
                    f"Step {step_id} contains an invalid editable filename."
                )

            if is_protected_file(filename):
                raise PipelineError(
                    f"Step {step_id} attempts to edit protected file: " f"{filename}"
                )

            path = Path(filename)

            if path.is_absolute() or ".." in path.parts:
                raise PipelineError(
                    f"Step {step_id} contains an unsafe path: {filename}"
                )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "task"


def create_worktree(plan: dict[str, Any]) -> tuple[Path, str]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(plan["name"])

    branch = f"ai/{slug}-{timestamp}"
    worktree = WORKTREE_ROOT / f"{slug}-{timestamp}"

    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            plan["base_branch"],
        ]
    )

    source_venv = ROOT / ".venv"
    target_venv = worktree / ".venv"

    if not source_venv.exists():
        raise PipelineError(f"Shared virtual environment is missing: {source_venv}")

    target_venv.symlink_to(source_venv, target_is_directory=True)

    return worktree, branch


def verify(worktree: Path) -> None:
    run(["make", "verify"], cwd=worktree)


def verify_declared_files_exist(
    worktree: Path,
    step: dict[str, Any],
) -> None:
    for filename in step["editable_files"]:
        if not (worktree / filename).is_file():
            raise PipelineError(
                f"Step {step['id']} references a missing file: {filename}"
            )


def check_changed_files(
    worktree: Path,
    allowed_files: list[str],
) -> None:
    changed = output(
        ["git", "status", "--porcelain"],
        cwd=worktree,
    )

    unexpected: list[str] = []

    for line in changed.splitlines():
        if not line:
            continue

        filename = line[3:]

        if " -> " in filename:
            filename = filename.split(" -> ", maxsplit=1)[1]

        if filename not in allowed_files:
            unexpected.append(filename)

    if unexpected:
        formatted = "\n".join(f"- {name}" for name in unexpected)
        raise PipelineError(
            "The model changed files outside the approved scope:\n" f"{formatted}"
        )


def execute_step(
    worktree: Path,
    step: dict[str, Any],
) -> None:
    step_id = step["id"]
    title = step["title"]
    files = step["editable_files"]

    print()
    print(f"=== Step {step_id}: {title} ===")

    verify_declared_files_exist(worktree, step)

    run(
        [
            "./run-editor.py",
            step["instruction"],
            *files,
        ],
        cwd=worktree,
    )

    check_changed_files(worktree, files)
    verify(worktree)

    run(["git", "add", "--", *files], cwd=worktree)

    staged_files = output(
        ["git", "diff", "--cached", "--name-only"],
        cwd=worktree,
    )

    if not staged_files:
        raise PipelineError(f"Step {step_id} produced no staged changes.")

    run(
        [
            "git",
            "commit",
            "-m",
            step["commit_message"],
        ],
        cwd=worktree,
    )


def execute_plan(plan_path: Path, *, dry_run: bool) -> None:
    plan = load_plan(
        plan_path,
        require_approval=not dry_run,
    )

    print(f"Plan: {plan['name']}")
    print(f"Approved: {plan['approved']}")
    print(f"Base branch: {plan['base_branch']}")
    print(f"Steps: {len(plan['steps'])}")
    print(f"Status: {plan['status']}")

    for step in plan["steps"]:
        print(
            f"  {step['id']}. {step['title']} " f"({', '.join(step['editable_files'])})"
        )

    if dry_run:
        print("\nDry run complete. No worktree was created.")
        return

    require_clean_repository()

    if plan["status"] == "already_satisfied":
        print()
        print("The current repository already satisfies the task.")
        print("No worktree or editing session is required.")
        return

    worktree: Path | None = None
    branch: str | None = None

    try:
        worktree, branch = create_worktree(plan)

        verify(worktree)

        for step in plan["steps"]:
            execute_step(worktree, step)

        verify(worktree)

    except Exception:
        if worktree is not None:
            print()
            print("Execution stopped. The worktree has been preserved for review:")
            print(f"  {worktree}")

        raise

    print()
    print("Plan completed successfully.")
    print(f"Branch: {branch}")
    print(f"Worktree: {worktree}")
    print()
    print("Review with:")
    print(f"  git -C {worktree} log --oneline")
    print(f"  git -C {worktree} diff {plan['base_branch']}...HEAD")
    print(f"  git -C {worktree} status --short")
    print()
    print("No changes have been merged into the base branch.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute an approved atomic coding plan."
    )

    parser.add_argument(
        "--plan",
        type=Path,
        default=PLAN_PATH,
        help="Path to the machine-readable plan.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and display the plan without executing it.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        execute_plan(args.plan.resolve(), dry_run=args.dry_run)
    except PipelineError as exc:
        print(f"Pipeline error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"Command failed with status {exc.returncode}.",
            file=sys.stderr,
        )
        return exc.returncode
    except KeyboardInterrupt:
        print("\nExecution interrupted.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
