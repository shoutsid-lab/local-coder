"""Run the committed-tree live E2E canary and emit a pasteable report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .state import StateStore

ROOT = Path(__file__).resolve().parents[1]
TASK = (
    "In profiles/live-e2e-canary.txt, replace exactly one occurrence of "
    '"LIVE_E2E_SENTINEL=before" with "LIVE_E2E_SENTINEL=after". '
    "Change no other text or file."
)
EXPECTED_FILE = "profiles/live-e2e-canary.txt"
SOURCE_SENTINEL = "LIVE_E2E_SENTINEL=before"
TARGET_SENTINEL = "LIVE_E2E_SENTINEL=after"
EXPECTED_SKILLS = [
    "atomic-implementation",
    "explore-repository",
    "plan-change",
    "review-change",
    "test-and-repair",
]


def command(
    args: list[str], *, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer local",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def edit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["edits"],
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "old_text", "new_text"],
                    "properties": {
                        "path": {"type": "string", "enum": [EXPECTED_FILE]},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                    },
                },
            }
        },
    }


def route_probe(route: str) -> None:
    response = post_json(
        "http://127.0.0.1:4000/v1/chat/completions",
        {
            "model": route,
            "messages": [{"role": "user", "content": "Reply with exactly ROUTE_OK."}],
            "temperature": 0,
            "max_tokens": 16,
        },
    )
    content = response["choices"][0]["message"]["content"].strip()
    usage = response.get("usage", {})
    if not content:
        raise RuntimeError(f"{route} returned empty content")
    if not isinstance(usage.get("prompt_tokens"), int) or not isinstance(
        usage.get("completion_tokens"), int
    ):
        raise RuntimeError(f"{route} did not return token usage")


def structured_output_probe(url: str, model: str, attempts: int) -> None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON object."},
            {
                "role": "user",
                "content": (
                    'Return exactly {"edits":[{"path":"profiles/'
                    'live-e2e-canary.txt","old_text":"LIVE_E2E_SENTINEL=before",'
                    '"new_text":"LIVE_E2E_SENTINEL=after"}]}. The top level '
                    "must contain only edits."
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 256,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "atomic_edits",
                "strict": True,
                "schema": edit_schema(),
            },
        },
    }
    for attempt in range(1, attempts + 1):
        response = post_json(url, payload)
        content = json.loads(response["choices"][0]["message"]["content"])
        if list(content) != ["edits"] or len(content["edits"]) != 1:
            raise RuntimeError(f"Structured-output probe failed on attempt {attempt}")
        edit = content["edits"][0]
        if set(edit) != {"path", "old_text", "new_text"}:
            raise RuntimeError(f"Structured-output shape failed on attempt {attempt}")
        if edit["path"] != EXPECTED_FILE:
            raise RuntimeError(f"Structured-output path failed on attempt {attempt}")


def dspy_role_backends(
    model_metrics: list[dict[str, Any]],
    *,
    route: str,
    source: str,
    program: str,
) -> list[str]:
    """Return unique audited DSPy backend markers for one fixed role."""
    backends: set[str] = set()
    for metric in model_metrics:
        metadata = metric.get("metadata")
        if not isinstance(metadata, str):
            continue
        try:
            parsed_metadata = json.loads(metadata)
        except json.JSONDecodeError:
            continue
        if (
            metric.get("route") == route
            and parsed_metadata.get("source") == source
            and parsed_metadata.get("program") == program
            and parsed_metadata.get("adapter") == "JSONAdapter"
        ):
            backends.add(f"{program}/JSONAdapter")
    return sorted(backends)


def dspy_explorer_backends(model_metrics: list[dict[str, Any]]) -> list[str]:
    """Return unique audited DSPy explorer backend markers."""
    return dspy_role_backends(
        model_metrics,
        route="local-plan",
        source="dspy-explorer",
        program="ExplorerProgram",
    )


def dspy_planner_backends(model_metrics: list[dict[str, Any]]) -> list[str]:
    """Return unique audited DSPy planner backend markers."""
    return dspy_role_backends(
        model_metrics,
        route="local-plan",
        source="dspy-planner",
        program="PlannerProgram",
    )


def dspy_implementer_backends(model_metrics: list[dict[str, Any]]) -> list[str]:
    """Return unique audited DSPy implementer backend markers."""
    return dspy_role_backends(
        model_metrics,
        route="local-fast",
        source="dspy-implementer",
        program="ImplementerProgram",
    )


def dspy_reviewer_backends(model_metrics: list[dict[str, Any]]) -> list[str]:
    """Return unique audited DSPy reviewer backend markers."""
    return dspy_role_backends(
        model_metrics,
        route="local-review",
        source="dspy-reviewer",
        program="ReviewerProgram",
    )


def check_skills() -> None:
    result = command([sys.executable, str(ROOT / "local-coder.py"), "skills"])
    skills = json.loads(result.stdout)
    names = sorted(skill["name"] for skill in skills)
    if names != EXPECTED_SKILLS:
        raise RuntimeError(f"Unexpected discovered skills: {names}")
    for skill in skills:
        if not skill["description"] or not skill["tools"] or skill["max_steps"] <= 0:
            raise RuntimeError(f"Incomplete skill binding: {skill['name']}")


def check_skills_lint_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="local-coder-skills-") as temporary:
        skills_root = Path(temporary) / "skills"
        shutil.copytree(ROOT / ".local-coder" / "skills", skills_root)
        skill_file = skills_root / "explore-repository" / "SKILL.md"
        with skill_file.open("a", encoding="utf-8") as handle:
            handle.write("\n[broken test link](references/DOES_NOT_EXIST.md)\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "runtime.skills_lint",
                str(skills_root),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            raise RuntimeError("skills-lint accepted a broken reference")
        diagnostic = f"{result.stdout}\n{result.stderr}"
        if "referenced resource does not exist" not in diagnostic:
            raise RuntimeError("skills-lint failed for an unexpected reason")


def stream_run(output_path: Path) -> int:
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "local-coder.py"),
            "run",
            "--max-steps",
            "12",
            "--expected-file",
            EXPECTED_FILE,
            TASK,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    with output_path.open("w", encoding="utf-8") as output:
        for line in process.stdout:
            print(line, end="")
            output.write(line)
    return process.wait()


def main() -> int:
    attempts = int(os.environ.get("LIVE_E2E_ATTEMPTS", "20"))
    if attempts <= 0:
        raise ValueError("LIVE_E2E_ATTEMPTS must be a positive integer")
    if command(["git", "status", "--porcelain"]).stdout.strip():
        raise RuntimeError("Live E2E requires a clean committed base repository.")
    original = (ROOT / EXPECTED_FILE).read_text(encoding="utf-8")
    if original.count(SOURCE_SENTINEL) != 1:
        raise RuntimeError("The live canary source sentinel must occur exactly once.")

    command(
        [sys.executable, str(ROOT / "local-coder.py"), "status"],
        capture=False,
    )
    check_skills()
    check_skills_lint_fails_closed()
    for route in ("local-fast", "local-plan", "local-review"):
        route_probe(route)
    structured_output_probe(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-coder",
        attempts,
    )
    structured_output_probe(
        "http://127.0.0.1:4000/v1/chat/completions",
        "local-fast",
        attempts,
    )

    store = StateStore(ROOT / ".local-coder" / "state" / "agent.db")
    previous = {run["id"] for run in store.recent_runs(limit=20)}
    staging = ROOT / ".local-coder" / "live-e2e"
    staging.mkdir(parents=True, exist_ok=True)
    console_path = staging / "latest-console.log"
    run_rc = stream_run(console_path)
    current = store.recent_runs(limit=20)
    run_id = next((run["id"] for run in current if run["id"] not in previous), None)
    if run_id is None:
        raise RuntimeError("Live E2E did not create an audited run.")
    details = store.run_details(run_id)
    assert details is not None
    run_dir = staging / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(details, indent=2), encoding="utf-8")
    console_path.replace(run_dir / "console.log")

    editor_calls = [
        call
        for call in details["tool_calls"]
        if call["tool_name"] == "apply_atomic_edit"
    ]
    failed_tools = [
        {
            "agent_role": call["agent_role"],
            "tool_name": call["tool_name"],
            "output": call["output"],
        }
        for call in details["tool_calls"]
        if call["status"] == "error"
    ]
    routes = sorted({metric["route"] for metric in details["model_metrics"]})
    explorer_backends = dspy_explorer_backends(details["model_metrics"])
    planner_backends = dspy_planner_backends(details["model_metrics"])
    implementer_backends = dspy_implementer_backends(details["model_metrics"])
    reviewer_backends = dspy_reviewer_backends(details["model_metrics"])
    worktree_value = details.get("worktree")
    worktree = Path(worktree_value) if worktree_value else None
    changed: list[str] = []
    edited = ""
    if worktree is not None and worktree.is_dir():
        changed_result = command(["git", "-C", str(worktree), "diff", "--name-only"])
        changed = changed_result.stdout.splitlines()
        canary = worktree / EXPECTED_FILE
        if canary.is_file():
            edited = canary.read_text(encoding="utf-8")
    passed = (
        run_rc == 0
        and details["status"] == "awaiting_approval"
        and details["error"] is None
        and len(editor_calls) == 1
        and editor_calls[0]["status"] == "success"
        and not failed_tools
        and routes == ["local-fast", "local-plan", "local-review"]
        and explorer_backends == ["ExplorerProgram/JSONAdapter"]
        and planner_backends == ["PlannerProgram/JSONAdapter"]
        and implementer_backends == ["ImplementerProgram/JSONAdapter"]
        and reviewer_backends == ["ReviewerProgram/JSONAdapter"]
        and changed == [EXPECTED_FILE]
        and TARGET_SENTINEL in edited
    )
    keep_worktree = os.environ.get("LIVE_E2E_KEEP_WORKTREE") == "1"
    summary = {
        "passed": passed,
        "base_commit": command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "run_id": run_id,
        "status": details["status"],
        "error": details["error"],
        "result_excerpt": str(details.get("result") or "")[-4000:],
        "worktree": details["worktree"],
        "branch": details["branch"],
        "editor_calls": [
            {"status": call["status"], "output": call["output"]}
            for call in editor_calls
        ],
        "failed_tools": failed_tools,
        "verification": [
            {
                "command": result["command"],
                "passed": result["passed"],
                "duration_ms": result["duration_ms"],
            }
            for result in details["verification"]
        ],
        "model_routes": routes,
        "explorer_backends": explorer_backends,
        "planner_backends": planner_backends,
        "implementer_backends": implementer_backends,
        "reviewer_backends": reviewer_backends,
        "changed_files": changed,
        "report": str(report_path),
        "console": str(run_dir / "console.log"),
        "worktree_preserved": not passed or keep_worktree,
    }
    summary_path = run_dir / "summary.json"
    summary_text = json.dumps(summary, indent=2) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    (staging / "latest-summary.json").write_text(summary_text, encoding="utf-8")
    if passed and not keep_worktree and worktree is not None:
        command(["git", "worktree", "remove", "--force", str(worktree)])
        command(["git", "branch", "-D", details["branch"]])
        command(["git", "worktree", "prune"])
    print("\nLIVE E2E SUMMARY")
    print(summary_text, end="")
    print(f"\nPaste this file for diagnosis: {staging / 'latest-summary.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        staging = ROOT / ".local-coder" / "live-e2e"
        staging.mkdir(parents=True, exist_ok=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        failure = {
            "passed": False,
            "base_commit": (commit.stdout.strip() if commit.returncode == 0 else None),
            "error": f"{type(exc).__name__}: {exc}",
        }
        (staging / "latest-summary.json").write_text(
            json.dumps(failure, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"live-e2e: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            f"Paste this file for diagnosis: {staging / 'latest-summary.json'}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
