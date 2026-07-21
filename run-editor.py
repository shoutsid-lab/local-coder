#!/usr/bin/env python3
"""Run one bounded validated edit through the local-fast route."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from runtime.editor import EditorError, request_and_apply

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction", help="One atomic editing instruction.")
    parser.add_argument("files", nargs="+", help="Approved existing editable files.")
    parser.add_argument(
        "--task",
        type=Path,
        default=None,
        help=(
            "Authoritative task file. Defaults to LOCAL_CODER_TASK_FILE; when neither "
            "is supplied, the atomic instruction is authoritative."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_path = args.task
    if task_path is None:
        configured = os.environ.get("LOCAL_CODER_TASK_FILE")
        task_path = Path(configured) if configured else None
    try:
        protected_files: set[str] = set()
        if task_path is None:
            task = f"# Atomic Task\n\n{args.instruction.strip()}\n"
        else:
            if not task_path.is_absolute():
                task_path = ROOT / task_path
            task = task_path.read_text(encoding="utf-8")
            protected_files.add(str(task_path.relative_to(ROOT)))
        changed = request_and_apply(
            root=ROOT,
            instruction=args.instruction,
            editable_files=args.files,
            task=task,
            protected_files=protected_files,
        )
    except (EditorError, FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
        print(f"Editor error: {exc}")
        return 1
    print(f"Changed files: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
