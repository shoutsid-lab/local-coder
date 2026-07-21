#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
LLAMA_HEALTH_URL = "http://127.0.0.1:8080/health"
LITELLM_HOST = "127.0.0.1"
LITELLM_PORT = 4000


def print_command(command: list[str]) -> None:
    print(f"+ {shlex.join(command)}", flush=True)


def run_command(command: list[str]) -> int:
    print_command(command)

    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
    )

    return result.returncode


def command_output(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    return result.returncode, result.stdout.strip()


def llama_server_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(
            LLAMA_HEALTH_URL,
            timeout=3,
        ) as response:
            data = json.load(response)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        return False

    return response.status == 200 and data.get("status") == "ok"


def litellm_is_available() -> bool:
    try:
        with socket.create_connection(
            (LITELLM_HOST, LITELLM_PORT),
            timeout=3,
        ):
            return True
    except OSError:
        return False


def handle_status(_: argparse.Namespace) -> int:
    llama_ok = llama_server_is_healthy()
    litellm_ok = litellm_is_available()

    branch_status, branch = command_output(
        ["git", "branch", "--show-current"]
    )
    tree_status, porcelain = command_output(
        ["git", "status", "--porcelain"]
    )

    branch = branch if branch_status == 0 and branch else "unknown"
    tree_clean = tree_status == 0 and not porcelain

    print(
        f"llama-server :8080  {'OK' if llama_ok else 'UNAVAILABLE'}"
    )
    print(
        f"LiteLLM      :4000  {'OK' if litellm_ok else 'UNAVAILABLE'}"
    )
    print(f"Git branch          {branch}")
    print(
        "Working tree        "
        f"{'clean' if tree_clean else 'has changes'}"
    )

    return 0 if llama_ok and litellm_ok and branch_status == 0 else 1


def handle_task(args: argparse.Namespace) -> int:
    return run_command(
        ["./run-aider.sh", "task", *args.files]
    )


def handle_repair(args: argparse.Namespace) -> int:
    return run_command(
        [
            "./run-aider.sh",
            "repair",
            args.instruction,
            *args.files,
        ]
    )


def handle_plan(_: argparse.Namespace) -> int:
    status = run_command(["./create-plan.py"])

    if status != 0:
        return status

    candidate = ROOT / "PLAN.candidate.json"

    if not candidate.is_file():
        print(
            "create-plan.py succeeded but PLAN.candidate.json "
            "was not created.",
            file=sys.stderr,
        )
        return 1

    print()
    print(candidate.read_text(encoding="utf-8"), end="")
    return 0


def handle_execute(_: argparse.Namespace) -> int:
    return run_command(["./run-plan.py"])


def handle_verify(_: argparse.Namespace) -> int:
    return run_command(["make", "verify"])


def handle_review(_: argparse.Namespace) -> int:
    return run_command(["./review-diff.py"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local AI coding pipeline CLI."
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Check local services and repository status.",
    )
    status_parser.set_defaults(handler=handle_status)

    task_parser = subparsers.add_parser(
        "task",
        help="Open an interactive Aider task.",
    )
    task_parser.add_argument(
        "files",
        nargs="+",
        help="Files Aider may edit.",
    )
    task_parser.set_defaults(handler=handle_task)

    repair_parser = subparsers.add_parser(
        "repair",
        help="Apply one atomic repair.",
    )
    repair_parser.add_argument(
        "instruction",
        help="Exact atomic editing instruction.",
    )
    repair_parser.add_argument(
        "files",
        nargs="+",
        help="Files Aider may edit.",
    )
    repair_parser.set_defaults(handler=handle_repair)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Generate and display an unapproved plan.",
    )
    plan_parser.set_defaults(handler=handle_plan)

    execute_parser = subparsers.add_parser(
        "execute",
        help="Execute the approved PLAN.json.",
    )
    execute_parser.set_defaults(handler=handle_execute)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Run deterministic verification.",
    )
    verify_parser.set_defaults(handler=handle_verify)

    review_parser = subparsers.add_parser(
        "review",
        help="Review the current Git diff.",
    )
    review_parser.set_defaults(handler=handle_review)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
