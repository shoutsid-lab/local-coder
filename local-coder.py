#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
STATE_PATH = ROOT / ".local-coder" / "state" / "agent.db"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def ensure_project_python() -> None:
    """Re-exec direct script invocations inside the project virtualenv."""
    if os.environ.get("LOCAL_CODER_VENV_BOOTSTRAPPED") == "1":
        return
    if not VENV_PYTHON.is_file():
        return
    try:
        already_using_venv = (
            Path(sys.prefix).resolve() == VENV_PYTHON.parent.parent.resolve()
        )
    except OSError:
        already_using_venv = False
    if already_using_venv:
        return

    environment = os.environ.copy()
    environment["LOCAL_CODER_VENV_BOOTSTRAPPED"] = "1"
    os.execve(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )


def print_command(command: list[str]) -> None:
    print(f"+ {shlex.join(command)}", flush=True)


def run_command(command: list[str]) -> int:
    print_command(command)
    result = subprocess.run(command, cwd=ROOT, check=False)
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
        with urllib.request.urlopen(LLAMA_HEALTH_URL, timeout=3) as response:
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
    smolagents_ok = importlib.util.find_spec("smolagents") is not None

    branch_status, branch = command_output(["git", "branch", "--show-current"])
    tree_status, porcelain = command_output(["git", "status", "--porcelain"])

    branch = branch if branch_status == 0 and branch else "unknown"
    tree_clean = tree_status == 0 and not porcelain

    print(f"llama-server :8080  {'OK' if llama_ok else 'UNAVAILABLE'}")
    print(f"LiteLLM      :4000  {'OK' if litellm_ok else 'UNAVAILABLE'}")
    print(f"smolagents          {'OK' if smolagents_ok else 'NOT INSTALLED'}")
    print(f"Python              {sys.executable}")
    print(f"Git branch          {branch}")
    print(f"Working tree        {'clean' if tree_clean else 'has changes'}")
    print(f"Run database        {STATE_PATH}")

    ready = llama_ok and litellm_ok and smolagents_ok and branch_status == 0
    return 0 if ready else 1


def handle_repair(args: argparse.Namespace) -> int:
    return run_command(
        [
            "./run-editor.py",
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
            "create-plan.py succeeded but PLAN.candidate.json was not created.",
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


def handle_run(args: argparse.Namespace) -> int:
    try:
        from runtime.orchestrator import AgentOrchestrator, OrchestratorConfig
    except ImportError as exc:
        print(f"Could not load agent runtime: {exc}", file=sys.stderr)
        return 1

    config = OrchestratorConfig(
        repository=ROOT,
        max_steps=args.max_steps,
        keep_worktree=True,
        mode="agentic",
    )
    summary = AgentOrchestrator(config).run(args.task)
    print(summary.to_json())
    completed = {
        "awaiting_approval",
        "needs_attention",
        "no_changes",
    }
    return 0 if summary.status in completed else 1


def handle_runs(args: argparse.Namespace) -> int:
    from runtime.state import StateStore

    rows = StateStore(STATE_PATH).recent_runs(limit=args.limit)
    print(json.dumps(rows, indent=2))
    return 0


def handle_show_run(args: argparse.Namespace) -> int:
    from runtime.state import StateStore

    details = StateStore(STATE_PATH).run_details(args.run_id)
    if details is None:
        print(f"Unknown run ID: {args.run_id}", file=sys.stderr)
        return 1
    print(json.dumps(details, indent=2))
    return 0


def handle_skills(_: argparse.Namespace) -> int:
    from runtime.skills import discover_skills

    skills = discover_skills(ROOT / ".local-coder" / "skills")
    payload = [
        {
            "name": skill.name,
            "description": skill.description,
            "model": skill.model,
            "tools": list(skill.tools),
            "max_steps": skill.max_steps,
        }
        for skill in skills.values()
    ]
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local AI coding pipeline CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status", help="Check local services and repository status."
    )
    status_parser.set_defaults(handler=handle_status)

    run_parser = subparsers.add_parser(
        "run", help="Run the role-separated agent harness in an isolated worktree."
    )
    run_parser.add_argument("task", help="Coding task for the orchestrator.")
    run_parser.add_argument(
        "--max-steps",
        type=int,
        default=12,
        help="Maximum manager-agent steps.",
    )
    run_parser.set_defaults(handler=handle_run)

    runs_parser = subparsers.add_parser("runs", help="List recent audited runs.")
    runs_parser.add_argument("--limit", type=int, default=20)
    runs_parser.set_defaults(handler=handle_runs)

    show_parser = subparsers.add_parser("show-run", help="Show one audited run.")
    show_parser.add_argument("run_id")
    show_parser.set_defaults(handler=handle_show_run)

    skills_parser = subparsers.add_parser("skills", help="List loaded agent skills.")
    skills_parser.set_defaults(handler=handle_skills)

    repair_parser = subparsers.add_parser(
        "repair", help="Apply one validated native atomic edit."
    )
    repair_parser.add_argument("instruction", help="Exact atomic editing instruction.")
    repair_parser.add_argument("files", nargs="+", help="Approved files to edit.")
    repair_parser.set_defaults(handler=handle_repair)

    plan_parser = subparsers.add_parser(
        "plan", help="Generate and display an unapproved plan."
    )
    plan_parser.set_defaults(handler=handle_plan)

    execute_parser = subparsers.add_parser(
        "execute", help="Execute the approved PLAN.json."
    )
    execute_parser.set_defaults(handler=handle_execute)

    verify_parser = subparsers.add_parser(
        "verify", help="Run deterministic verification."
    )
    verify_parser.set_defaults(handler=handle_verify)

    review_parser = subparsers.add_parser("review", help="Review the current Git diff.")
    review_parser.set_defaults(handler=handle_review)

    return parser


def main() -> int:
    ensure_project_python()
    parser = build_parser()
    args = parser.parse_args()
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
