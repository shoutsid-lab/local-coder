"""Narrow repository tools exposed to local agents."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .state import StateStore


@dataclass(frozen=True)
class Worktree:
    """An isolated Git worktree allocated to an agent run."""

    path: Path
    branch: str
    base_branch: str


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:48] or "task"


def command(
    args: list[str],
    *,
    cwd: Path,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without invoking a shell."""
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env=env,
    )


def collect_uncommitted_diff(root: Path) -> str:
    """Return tracked and untracked changes as a unified diff."""
    sections: list[str] = []
    tracked = command(["git", "diff", "--no-ext-diff"], cwd=root)
    if tracked.returncode != 0:
        raise RuntimeError(tracked.stderr.strip() or "Could not inspect Git diff.")
    if tracked.stdout.strip():
        sections.append(tracked.stdout.strip())

    untracked = command(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
    if untracked.returncode != 0:
        raise RuntimeError(
            untracked.stderr.strip() or "Could not list untracked files."
        )
    for relative in untracked.stdout.splitlines():
        file_path = root / relative
        if not file_path.is_file():
            continue
        result = command(
            ["git", "diff", "--no-index", "--", "/dev/null", relative],
            cwd=root,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr.strip() or f"Could not diff {relative}.")
        if result.stdout.strip():
            sections.append(result.stdout.strip())

    return "\n\n".join(sections)


def require_clean_repository(root: Path) -> None:
    result = command(["git", "status", "--porcelain"], cwd=root, check=True)
    if result.stdout.strip():
        raise RuntimeError("The base repository has uncommitted changes.")


def current_branch(root: Path) -> str:
    result = command(["git", "branch", "--show-current"], cwd=root, check=True)
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("The repository is in detached HEAD state.")
    return branch


def create_worktree(root: Path, *, run_id: str, task: str) -> Worktree:
    """Create an isolated worktree and share the repository virtualenv."""
    require_clean_repository(root)
    base_branch = current_branch(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{_slug(task)}-{run_id}-{stamp}"
    branch = f"agent/{name}"
    parent = root.parent / f"{root.name}-worktrees"
    path = parent / name
    parent.mkdir(parents=True, exist_ok=True)
    result = command(
        ["git", "worktree", "add", "-b", branch, str(path), base_branch],
        cwd=root,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not create worktree.")
    source_venv = root / ".venv"
    target_venv = path / ".venv"
    if source_venv.exists() and not target_venv.exists():
        target_venv.symlink_to(source_venv, target_is_directory=True)
    return Worktree(path=path, branch=branch, base_branch=base_branch)


def remove_worktree(root: Path, worktree: Worktree, *, force: bool = False) -> None:
    """Remove an allocated worktree and its task branch."""
    args = ["git", "worktree", "remove", str(worktree.path)]
    if force:
        args.append("--force")
    result = command(args, cwd=root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not remove worktree.")
    command(["git", "branch", "-D", worktree.branch], cwd=root)


@dataclass
class ToolContext:
    """Execution context shared by all agent tools in one worktree."""

    root: Path
    worktree: Worktree
    run_id: str
    state: StateStore
    task_file: Path
    agent_role: str | None = None
    last_review_verdict: str | None = None

    def _safe_path(self, relative: str) -> Path:
        candidate = (self.worktree.path / relative).resolve()
        root = self.worktree.path.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes the worktree: {relative}")
        return candidate

    def _recorded(
        self,
        tool_name: str,
        arguments: Any,
        operation: Callable[[], str],
    ) -> str:
        started = time.perf_counter()
        status = "success"
        output = ""
        try:
            output = operation()
            return output
        except Exception as exc:
            status = "error"
            output = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.state.log_tool_call(
                self.run_id,
                agent_role=self.agent_role,
                tool_name=tool_name,
                arguments=arguments,
                output=output[-50000:],
                status=status,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    def read_file(self, path: str, start_line: int = 1, end_line: int = 240) -> str:
        """Read a UTF-8 text file from the worktree."""

        def operation() -> str:
            file_path = self._safe_path(path)
            if not file_path.is_file():
                raise FileNotFoundError(path)
            lines = file_path.read_text(encoding="utf-8").splitlines()
            start = max(1, start_line)
            end = max(start, min(end_line, start + 500))
            selected = lines[start - 1 : end]
            return "\n".join(
                f"{number}: {line}" for number, line in enumerate(selected, start=start)
            )

        return self._recorded(
            "read_file",
            {"path": path, "start_line": start_line, "end_line": end_line},
            operation,
        )

    def list_files(self, pattern: str = "*") -> str:
        """List tracked files matching a glob pattern."""

        def operation() -> str:
            result = command(["git", "ls-files", pattern], cwd=self.worktree.path)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            return result.stdout.strip() or "No tracked files matched."

        return self._recorded("list_files", {"pattern": pattern}, operation)

    def search_repository(self, query: str, pattern: str = "*") -> str:
        """Search tracked UTF-8 files without invoking a shell."""

        def operation() -> str:
            file_result = command(["git", "ls-files", pattern], cwd=self.worktree.path)
            if file_result.returncode != 0:
                raise RuntimeError(file_result.stderr.strip())
            matches: list[str] = []
            for relative in file_result.stdout.splitlines():
                path = self._safe_path(relative)
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(lines, start=1):
                    if query.lower() in line.lower():
                        matches.append(f"{relative}:{number}:{line}")
                        if len(matches) >= 100:
                            return "\n".join(matches)
            return "\n".join(matches) or "No matches found."

        return self._recorded(
            "search_repository", {"query": query, "pattern": pattern}, operation
        )

    def git_status(self) -> str:
        """Return the concise worktree status."""

        def operation() -> str:
            result = command(["git", "status", "--short"], cwd=self.worktree.path)
            return result.stdout.strip() or "Working tree clean."

        return self._recorded("git_status", {}, operation)

    def inspect_diff(self) -> str:
        """Return the full uncommitted Git diff."""

        def operation() -> str:
            return (
                collect_uncommitted_diff(self.worktree.path) or "No uncommitted diff."
            )

        return self._recorded("inspect_diff", {}, operation)

    def delegate_aider(self, instruction: str, editable_files: str) -> str:
        """Delegate one atomic code edit to the proven Aider worker."""

        def operation() -> str:
            files = [item.strip() for item in editable_files.split(",") if item.strip()]
            if not files:
                raise ValueError("At least one editable file is required.")
            editable_paths = [self._safe_path(item) for item in files]
            missing = [
                item for item, path in zip(files, editable_paths) if not path.is_file()
            ]
            if missing:
                raise FileNotFoundError(
                    f"Editable files must already exist: {', '.join(missing)}"
                )
            before = [path.read_bytes() for path in editable_paths]
            env = dict(__import__("os").environ)
            env["LOCAL_CODER_TASK_FILE"] = str(self.task_file)
            exact_paths = ", ".join(files)
            bounded_instruction = (
                f"Edit only these existing repository paths exactly as named: "
                f"{exact_paths}. Never create a path/to/ placeholder. {instruction}"
            )
            result = command(
                ["./run-aider.sh", "apply", bounded_instruction, *files],
                cwd=self.worktree.path,
                env=env,
            )
            combined = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            if result.returncode != 0:
                raise RuntimeError(combined or "Aider edit failed.")
            if all(
                path.read_bytes() == original
                for path, original in zip(editable_paths, before)
            ):
                raise RuntimeError(
                    "Aider reported success but did not change an editable file."
                )
            return combined or "Aider edit completed."

        return self._recorded(
            "delegate_aider",
            {"instruction": instruction, "editable_files": editable_files},
            operation,
        )

    def run_verification(self) -> str:
        """Run the repository's deterministic verification command."""

        def operation() -> str:
            started = time.perf_counter()
            result = command(["make", "verify"], cwd=self.worktree.path)
            combined = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            self.state.add_verification(
                self.run_id,
                command="make verify",
                passed=result.returncode == 0,
                output=combined,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            status = "PASS" if result.returncode == 0 else "FAIL"
            return f"Verification: {status}\n{combined}".strip()

        return self._recorded("run_verification", {}, operation)

    def review_diff(self) -> str:
        """Run the read-only semantic reviewer against the worktree branch."""

        def operation() -> str:
            run_dir = self.worktree.path / ".local-coder" / "runs" / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            output_path = run_dir / "REVIEW.json"
            result = command(
                [
                    sys.executable,
                    "./review-diff.py",
                    "--task",
                    str(self.task_file),
                    "--output",
                    str(output_path),
                ],
                cwd=self.worktree.path,
            )
            combined = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            if output_path.exists():
                review_text = output_path.read_text(encoding="utf-8")
                self.state.add_artifact(
                    self.run_id,
                    kind="review",
                    path=output_path,
                    content=review_text,
                )
                try:
                    self.last_review_verdict = json.loads(review_text).get("verdict")
                except json.JSONDecodeError:
                    self.last_review_verdict = None
            return combined or f"Reviewer exited with status {result.returncode}."

        return self._recorded("review_diff", {}, operation)

    def rollback_worktree(self) -> str:
        """Discard uncommitted edits in the isolated worktree."""

        def operation() -> str:
            command(
                ["git", "reset", "--hard", "HEAD"], cwd=self.worktree.path, check=True
            )
            command(["git", "clean", "-fd"], cwd=self.worktree.path, check=True)
            return "Worktree restored to HEAD."

        return self._recorded("rollback_worktree", {}, operation)


def build_smol_tools(context: ToolContext, names: tuple[str, ...]) -> list[Any]:
    """Wrap selected context methods as smolagents tools."""
    try:
        from smolagents import tool
    except ImportError as exc:
        raise RuntimeError(
            "smolagents is not installed. Run `make agent-install`."
        ) from exc

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 240) -> str:
        """Read a text file from the isolated worktree.

        Args:
            path: Repository-relative file path.
            start_line: First 1-based line to read.
            end_line: Last 1-based line to read, capped to 500 lines.
        """
        return context.read_file(path, start_line, end_line)

    @tool
    def list_files(pattern: str = "*") -> str:
        """List tracked repository files.

        Args:
            pattern: Git path glob, such as '*.py' or 'src/*'.
        """
        return context.list_files(pattern)

    @tool
    def search_repository(query: str, pattern: str = "*") -> str:
        """Search tracked text files for a case-insensitive string.

        Args:
            query: Text to locate.
            pattern: Git path glob restricting searched files.
        """
        return context.search_repository(query, pattern)

    @tool
    def git_status() -> str:
        """Return the concise Git status for the isolated worktree."""
        return context.git_status()

    @tool
    def inspect_diff() -> str:
        """Return the current uncommitted Git diff."""
        return context.inspect_diff()

    @tool
    def delegate_aider(instruction: str, editable_files: str) -> str:
        """Apply one exact atomic edit through Aider.

        Args:
            instruction: One narrowly scoped editing instruction.
            editable_files: Comma-separated repository-relative file paths.
        """
        return context.delegate_aider(instruction, editable_files)

    @tool
    def run_verification() -> str:
        """Run deterministic formatting, lint, and test verification."""
        return context.run_verification()

    @tool
    def review_diff() -> str:
        """Ask the read-only local reviewer to assess the branch diff."""
        return context.review_diff()

    @tool
    def rollback_worktree() -> str:
        """Discard all uncommitted edits in the isolated worktree."""
        return context.rollback_worktree()

    available = {
        item.name: item
        for item in (
            read_file,
            list_files,
            search_repository,
            git_status,
            inspect_diff,
            delegate_aider,
            run_verification,
            review_diff,
            rollback_worktree,
        )
    }
    unknown = set(names) - available.keys()
    if unknown:
        raise ValueError(f"Unknown tool names requested by skill: {sorted(unknown)}")
    return [available[name] for name in names]
