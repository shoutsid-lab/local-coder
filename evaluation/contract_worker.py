#!/usr/bin/env python3
"""Base-owned scenario worker; emits observations but contains no oracle answers."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _load_candidate(candidate: Path) -> tuple[Any, Any]:
    sys.path.insert(0, str(candidate))
    from runtime import editor, tools

    return editor, tools


def _error_name(operation: Any) -> str | None:
    try:
        operation()
    except Exception as exc:
        return type(exc).__name__
    return None


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def run_scenario(candidate: Path, scenario: str) -> dict[str, Any]:
    """Execute one deterministic scenario against candidate implementation code."""
    editor, tools = _load_candidate(candidate)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        if scenario == "exact_edit":
            target = root / "sample.txt"
            target.write_text("before\n", encoding="utf-8")
            changed = editor.apply_edits(
                root,
                ["sample.txt"],
                [editor.AtomicEdit("sample.txt", "before", "after")],
            )
            return {"changed": changed, "content": target.read_text(encoding="utf-8")}
        if scenario == "multi_edit_atomicity":
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first before\n", encoding="utf-8")
            second.write_text("second before\n", encoding="utf-8")
            error = _error_name(
                lambda: editor.apply_edits(
                    root,
                    ["first.txt", "second.txt"],
                    [
                        editor.AtomicEdit("first.txt", "first before", "first after"),
                        editor.AtomicEdit("second.txt", "missing", "second after"),
                    ],
                )
            )
            return {
                "error": error,
                "first": first.read_text(encoding="utf-8"),
                "second": second.read_text(encoding="utf-8"),
            }
        if scenario in {"missing_match", "ambiguous_match"}:
            target = root / "sample.txt"
            content = "same\nsame\n" if scenario == "ambiguous_match" else "before\n"
            old_text = "same" if scenario == "ambiguous_match" else "missing"
            target.write_text(content, encoding="utf-8")
            error = _error_name(
                lambda: editor.apply_edits(
                    root,
                    ["sample.txt"],
                    [editor.AtomicEdit("sample.txt", old_text, "after")],
                )
            )
            return {"error": error, "content": target.read_text(encoding="utf-8")}
        if scenario == "scope_leakage":
            approved = root / "approved.txt"
            outside = root / "outside.txt"
            approved.write_text("approved\n", encoding="utf-8")
            outside.write_text("outside\n", encoding="utf-8")
            error = _error_name(
                lambda: editor.apply_edits(
                    root,
                    ["approved.txt"],
                    [editor.AtomicEdit("outside.txt", "outside", "changed")],
                )
            )
            return {"error": error, "outside": outside.read_text(encoding="utf-8")}
        if scenario == "malformed_editor_output":
            return {"error": _error_name(lambda: editor.parse_edit_content("not-json"))}
        if scenario == "malformed_reviewer_output":
            path = candidate / "review-diff.py"
            spec = importlib.util.spec_from_file_location("candidate_review", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("Could not load candidate reviewer.")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return {
                "error": _error_name(lambda: module.parse_review_content("not-json"))
            }
        if scenario == "verification_failure":
            result = subprocess.run(
                [sys.executable, "-c", "raise SystemExit(7)"],
                check=False,
            )
            return {"returncode": result.returncode}
        if scenario in {"complete_diff_capture", "empty_untracked_diff"}:
            _git(["init", "-q"], root)
            _git(["config", "user.email", "evaluation@example.com"], root)
            _git(["config", "user.name", "Evaluation"], root)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            _git(["add", "tracked.txt"], root)
            _git(["commit", "-qm", "baseline"], root)
            if scenario == "empty_untracked_diff":
                (root / "empty.py").touch()
                diff = tools.collect_uncommitted_diff(root)
                return {"has_empty": "empty.py" in diff}
            tracked.write_text("after\n", encoding="utf-8")
            (root / "untracked.py").write_text("value = 1\n", encoding="utf-8")
            (root / "linked").symlink_to("tracked.txt")
            diff = tools.collect_uncommitted_diff(root)
            return {
                "tracked": "tracked.txt" in diff and "+after" in diff,
                "untracked": "untracked.py" in diff and "+value = 1" in diff,
                "symlink": "new file mode 120000" in diff,
            }
        if scenario == "sequential_edits":
            target = root / "sample.txt"
            target.write_text("a b\n", encoding="utf-8")
            changed = editor.apply_edits(
                root,
                ["sample.txt"],
                [
                    editor.AtomicEdit("sample.txt", "a", "A"),
                    editor.AtomicEdit("sample.txt", "b", "B"),
                ],
            )
            return {"changed": changed, "content": target.read_text(encoding="utf-8")}
        if scenario == "protected_alias":
            target = root / "Makefile"
            target.write_text("before\n", encoding="utf-8")
            error = _error_name(
                lambda: editor.apply_edits(
                    root,
                    ["./Makefile"],
                    [editor.AtomicEdit("./Makefile", "before", "after")],
                )
            )
            return {"error": error, "content": target.read_text(encoding="utf-8")}
    raise ValueError(f"Unknown scenario: {scenario}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run_scenario(args.candidate.resolve(), args.scenario), sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
