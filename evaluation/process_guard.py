"""Trusted exec wrapper that installs a kernel process-count ceiling."""

from __future__ import annotations

import argparse
import os
import resource


def main() -> int:
    """Set an immutable RLIMIT_NPROC and replace this process with the command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-processes", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.max_processes < 1 or not args.command:
        parser.error("a positive process limit and command are required")
    command = args.command[1:] if args.command[0] == "--" else args.command
    resource.setrlimit(
        resource.RLIMIT_NPROC,
        (args.max_processes, args.max_processes),
    )
    os.execv(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
