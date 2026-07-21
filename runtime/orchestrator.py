"""Top-level isolated local coding-agent orchestration."""

from __future__ import annotations

import json
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agents import build_agent_bundle
from .models import ModelRegistry
from .skills import discover_skills
from .state import StateStore
from .tools import ToolContext, Worktree, create_worktree, current_branch


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for one agentic coding run."""

    repository: Path
    max_steps: int = 12
    keep_worktree: bool = True
    mode: str = "agentic"


@dataclass(frozen=True)
class RunSummary:
    """Inspection-ready outcome of an orchestrated run."""

    run_id: str
    status: str
    branch: str | None
    worktree: str | None
    verification_passed: bool
    result: str
    review_verdict: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class AgentOrchestrator:
    """Create an isolated run and coordinate role-specialized local agents."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.root = config.repository.resolve()
        self.state = StateStore(self.root / ".local-coder" / "state" / "agent.db")
        self.models = ModelRegistry()

    def _service_preflight(self) -> None:
        if not self.models.llama_available():
            raise RuntimeError("llama-server is not healthy on port 8080.")
        if not self.models.litellm_available():
            raise RuntimeError("LiteLLM is not listening on port 4000.")

    def run(self, task: str) -> RunSummary:
        if self.config.mode != "agentic":
            raise ValueError("Only agentic mode is currently supported.")
        self._service_preflight()
        base_branch = current_branch(self.root)
        run_id = self.state.create_run(
            task=task,
            mode=self.config.mode,
            repository=self.root,
            base_branch=base_branch,
        )
        worktree: Worktree | None = None
        try:
            worktree = create_worktree(self.root, run_id=run_id, task=task)
            self.state.update_run(
                run_id,
                status="running",
                branch=worktree.branch,
                worktree=str(worktree.path),
            )
            run_dir = worktree.path / ".local-coder" / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            task_file = run_dir / "TASK.md"
            task_file.write_text(
                "# Agent Task\n\n" + task.strip() + "\n",
                encoding="utf-8",
            )
            self.state.add_artifact(run_id, kind="task", path=task_file, content=task)

            context = ToolContext(
                root=self.root,
                worktree=worktree,
                run_id=run_id,
                state=self.state,
                task_file=task_file,
                agent_role="orchestrator",
            )
            skills = discover_skills(worktree.path / ".local-coder" / "skills")
            bundle = build_agent_bundle(
                skills=skills,
                context=context,
                models=self.models,
                state=self.state,
                run_id=run_id,
                manager_max_steps=self.config.max_steps,
            )
            prompt = f"""
Complete this coding task inside the already-created isolated worktree:

{task}

Required process:
1. Delegate repository inspection to the explorer.
2. Delegate an atomic, ordered implementation plan to the planner.
3. Delegate each narrowly scoped edit to the implementer, which must use Aider.
4. If deterministic verification fails, delegate one failure at a time to the repairer.
5. Run deterministic verification and delegate final diff review to the reviewer.
6. Never commit, merge, modify contract tests, or weaken acceptance criteria.
7. Finish with the branch, worktree, changed files, verification result,
   and review verdict.
""".strip()
            result: Any = bundle.manager.run(prompt, reset=True)
            verification_output = context.run_verification()
            verification_passed = verification_output.startswith("Verification: PASS")
            diff = context.inspect_diff()
            review_verdict: str | None = None
            if diff != "No uncommitted diff.":
                self.state.add_artifact(run_id, kind="diff", content=diff)
                review_output = context.review_diff()
                review_verdict = context.last_review_verdict
            else:
                review_output = "No diff was produced; semantic review skipped."

            if not verification_passed:
                status = "failed_verification"
            elif diff == "No uncommitted diff.":
                status = "no_changes"
            elif review_verdict == "pass":
                status = "awaiting_approval"
            else:
                status = "needs_attention"

            result_text = (
                f"{result}\n\n{verification_output}\n\n{review_output}".strip()
            )
            self.state.update_run(run_id, status=status, result=result_text)
            return RunSummary(
                run_id=run_id,
                status=status,
                branch=worktree.branch,
                worktree=str(worktree.path),
                verification_passed=verification_passed,
                result=result_text,
                review_verdict=review_verdict,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.state.update_run(
                run_id,
                status="failed",
                error=error,
                result={"traceback": traceback.format_exc()},
            )
            return RunSummary(
                run_id=run_id,
                status="failed",
                branch=worktree.branch if worktree else None,
                worktree=str(worktree.path) if worktree else None,
                verification_passed=False,
                result=(
                    "The agent run failed. The worktree is preserved for inspection."
                ),
                review_verdict=None,
                error=error,
            )
