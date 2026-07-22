"""Bounded, base-owned paired evaluation of committed generations."""

from __future__ import annotations

import json
import math
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from runtime.state import StateStore
from runtime.editor import PIPELINE_CONTROLS, PIPELINE_CONTROL_PREFIXES

from .manifests import EvaluationCase, SuiteManifest
from .outcomes import hash_text, stable_hash


class EvaluationError(RuntimeError):
    """Raised when trusted evaluation cannot proceed safely."""


@dataclass(frozen=True)
class EvaluationBudget:
    """Hard limits for one paired evaluation."""

    campaign_wall_seconds: int = 1800
    process_wall_seconds: int = 180
    max_processes: int = 64
    max_output_bytes: int = 1_000_000
    max_memory_mb: int = 2048
    max_file_mb: int = 64
    max_disk_mb: int = 512
    max_prompt_tokens: int = 200_000
    max_completion_tokens: int = 100_000
    max_model_calls: int = 64

    def validate(self) -> None:
        """Reject missing, non-positive, or impractically broad limits."""
        values = asdict(self)
        if any(not isinstance(value, int) or value <= 0 for value in values.values()):
            raise EvaluationError("Every evaluation budget must be a positive integer.")
        if self.campaign_wall_seconds > 7200 or self.process_wall_seconds > 1800:
            raise EvaluationError("Evaluation time budget exceeds the trusted ceiling.")
        if (
            self.max_processes > 256
            or self.max_memory_mb > 8192
            or self.max_disk_mb > 4096
            or self.max_model_calls > 256
        ):
            raise EvaluationError(
                "Evaluation resource budget exceeds the trusted ceiling."
            )


@dataclass(frozen=True)
class ProcessResult:
    """Recorded result of one bounded non-shell process."""

    command: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    duration_ms: float
    stdout: str
    stderr: str
    output_truncated: bool


@dataclass(frozen=True)
class CaseResult:
    """One oracle comparison for one generation and repetition."""

    generation: str
    repetition: int
    case_id: str
    visibility: str
    process: ProcessResult
    observation_hash: str | None
    oracle_passed: bool
    policy_passed: bool
    failure: str | None

    def to_dict(self, *, redact_holdout: bool = True) -> dict[str, Any]:
        """Serialize without exposing holdout process output by default."""
        value = asdict(self)
        if redact_holdout and self.visibility == "holdout":
            value["process"]["stdout"] = "<redacted holdout output>"
            value["process"]["stderr"] = "<redacted holdout output>"
        return value


@dataclass(frozen=True)
class PairedEvaluation:
    """Complete paired evidence under one immutable environment and suite."""

    baseline_commit: str
    candidate_commit: str
    development_suite_hash: str
    holdout_suite_hash: str
    holdout_oracle_hash: str
    environment_hash: str
    candidate_patch_hash: str
    repetitions: int
    budget: EvaluationBudget
    results: tuple[CaseResult, ...]
    evaluation_id: str | None = None
    build_id: str | None = None

    def to_dict(self, *, redact_holdout: bool = True) -> dict[str, Any]:
        """Return an inspection-ready, optionally redacted report."""
        return {
            "baseline_commit": self.baseline_commit,
            "candidate_commit": self.candidate_commit,
            "development_suite_hash": self.development_suite_hash,
            "holdout_suite_hash": self.holdout_suite_hash,
            "holdout_oracle_hash": self.holdout_oracle_hash,
            "environment_hash": self.environment_hash,
            "candidate_patch_hash": self.candidate_patch_hash,
            "repetitions": self.repetitions,
            "budget": asdict(self.budget),
            "evaluation_id": self.evaluation_id,
            "build_id": self.build_id,
            "results": [
                result.to_dict(redact_holdout=redact_holdout) for result in self.results
            ],
        }


def _git_bytes(root: Path, *args: str, max_bytes: int = 1_000_000) -> bytes:
    """Run one read-only Git inspection without buffering unbounded output."""

    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
        os.setsid()

    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        process = subprocess.Popen(
            ["git", *args],
            cwd=root,
            stdout=stdout_file,
            stderr=stderr_file,
            preexec_fn=limits,
        )
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise EvaluationError("Git inspection timed out.") from exc
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(max_bytes)
        stderr = stderr_file.read(max_bytes).decode("utf-8", "replace").strip()
    if stdout_size + stderr_size >= max_bytes:
        raise EvaluationError("Git inspection exceeded its output budget.")
    if process.returncode != 0:
        raise EvaluationError(stderr or "Git inspection failed.")
    return stdout


def _git_output(root: Path, *args: str, max_bytes: int = 1_000_000) -> str:
    return (
        _git_bytes(root, *args, max_bytes=max_bytes).decode("utf-8", "replace").strip()
    )


def committed_generation(root: Path) -> str:
    """Return HEAD only for an existing clean Git generation."""
    root = root.resolve()
    if not root.is_dir():
        raise EvaluationError(f"Generation directory does not exist: {root}")
    if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise EvaluationError(f"Generation must be committed and clean: {root}")
    return _git_output(root, "rev-parse", "HEAD")


def candidate_patch(
    candidate: Path,
    *,
    baseline_commit: str,
    max_bytes: int,
) -> tuple[str, str]:
    """Require direct Git lineage and return the archived candidate patch and hash."""
    try:
        _git_bytes(
            candidate,
            "merge-base",
            "--is-ancestor",
            baseline_commit,
            "HEAD",
            max_bytes=4096,
        )
    except EvaluationError:
        raise EvaluationError("Candidate commit does not descend from the baseline.")
    patch = _git_bytes(
        candidate,
        "diff",
        "--binary",
        baseline_commit,
        "HEAD",
        "--",
        max_bytes=max_bytes,
    ).decode("utf-8", "replace")
    return patch, hash_text(patch)


def generation_changed_paths(candidate: Path, baseline_commit: str) -> tuple[str, ...]:
    """Return all candidate paths changed from its baseline commit."""
    output = _git_bytes(
        candidate,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        baseline_commit,
        "HEAD",
        "--",
    )
    return tuple(sorted(path.decode("utf-8") for path in output.split(b"\0") if path))


def environment_identity(trusted_root: Path) -> tuple[dict[str, Any], str]:
    """Fingerprint the evaluator environment and stable service configuration."""
    files: dict[str, str | None] = {}
    for relative in (
        "litellm-config.yaml",
        "requirements-agent.txt",
        "evaluation/contract_worker.py",
        "evaluation/process_guard.py",
        "evaluation/supervisor.py",
    ):
        path = trusted_root / relative
        files[relative] = (
            hash_text(path.read_text(encoding="utf-8")) if path.is_file() else None
        )
    identity = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "executable": str(Path(sys.executable).resolve()),
        "bwrap": shutil.which("bwrap"),
        "configuration": files,
    }
    return identity, stable_hash(identity)


class Supervisor:
    """Execute trusted contract workers with fail-closed resource enforcement."""

    def __init__(self, trusted_root: Path, budget: EvaluationBudget) -> None:
        self.trusted_root = trusted_root.resolve()
        self.budget = budget
        self.budget.validate()
        self.bwrap = shutil.which("bwrap")
        if self.bwrap is None:
            raise EvaluationError("bubblewrap is required for candidate evaluation.")
        self.process_count = 0
        self.started = time.monotonic()

    def _limits(self) -> None:
        cpu_seconds = max(1, math.ceil(self.budget.process_wall_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        memory = self.budget.max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        file_size = self.budget.max_file_mb * 1024 * 1024
        file_size = min(file_size, self.budget.max_output_bytes)
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
        os.setsid()

    def _run(self, command: list[str]) -> ProcessResult:
        if self.process_count >= self.budget.max_processes:
            raise EvaluationError("Evaluation process budget exhausted.")
        elapsed = time.monotonic() - self.started
        if elapsed >= self.budget.campaign_wall_seconds:
            raise EvaluationError("Evaluation campaign wall-time budget exhausted.")
        self.process_count += 1
        timeout = min(
            self.budget.process_wall_seconds,
            self.budget.campaign_wall_seconds - elapsed,
        )
        started = time.perf_counter()
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                preexec_fn=self._limits,
            )
            timed_out = False
            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            limit = self.budget.max_output_bytes
            truncated = stdout_size + stderr_size >= limit
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(limit // 2).decode("utf-8", "replace")
            stderr = stderr_file.read(limit // 2).decode("utf-8", "replace")
        return ProcessResult(
            command=tuple(command),
            returncode=None if timed_out else process.returncode,
            timed_out=timed_out,
            duration_ms=(time.perf_counter() - started) * 1000,
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated,
        )

    def run_contract(
        self,
        candidate: Path,
        case: EvaluationCase,
        *,
        generation: str,
        repetition: int,
        visibility: str,
        oracle: dict[str, Any],
    ) -> CaseResult:
        """Run one base-owned worker in a networkless, read-only mount namespace."""
        candidate = candidate.resolve()
        worker = self.trusted_root / "evaluation" / "contract_worker.py"
        process_guard = self.trusted_root / "evaluation" / "process_guard.py"
        command = [
            self.bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(self.budget.max_disk_mb * 1024 * 1024),
            "--perms",
            "1777",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/candidate",
            "--ro-bind",
            str(candidate / "runtime"),
            "/candidate/runtime",
            "--ro-bind",
            str(candidate / "review-diff.py"),
            "/candidate/review-diff.py",
            "--dir",
            "/trusted",
            "--ro-bind",
            str(worker),
            "/trusted/contract_worker.py",
            "--ro-bind",
            str(process_guard),
            "/trusted/process_guard.py",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--chdir",
            "/candidate",
            "/usr/bin/python3",
            "/trusted/process_guard.py",
            "--max-processes",
            str(self.budget.max_processes),
            "--",
            "/usr/bin/python3",
            "/trusted/contract_worker.py",
            "--candidate",
            "/candidate",
            "--scenario",
            case.scenario,
        ]
        process = self._run(command)
        observation: dict[str, Any] | None = None
        failure: str | None = None
        if process.timed_out:
            failure = "timeout"
        elif process.returncode != 0:
            failure = "process_exit"
        elif process.output_truncated:
            failure = "output_budget"
        else:
            try:
                parsed = json.loads(process.stdout)
                observation = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                observation = None
            if observation is None:
                failure = "malformed_observation"
            elif observation != oracle:
                failure = "oracle"
        return CaseResult(
            generation=generation,
            repetition=repetition,
            case_id=case.case_id,
            visibility=visibility,
            process=process,
            observation_hash=(
                stable_hash(observation) if observation is not None else None
            ),
            oracle_passed=failure is None,
            policy_passed=not process.timed_out and not process.output_truncated,
            failure=failure,
        )

    def run_path_policy(
        self,
        candidate: Path,
        *,
        baseline_commit: str,
        allowed_paths: set[str],
        generation: str,
        repetition: int,
    ) -> CaseResult:
        """Reject protected or undeclared candidate paths using trusted Git evidence."""
        changed = generation_changed_paths(candidate, baseline_commit)
        protected = [
            path
            for path in changed
            if path in PIPELINE_CONTROLS
            or path.startswith(PIPELINE_CONTROL_PREFIXES)
            or Path(path).name.endswith("_contract.py")
        ]
        unexpected = sorted(set(changed) - allowed_paths)
        violations = sorted(set(protected) | set(unexpected))
        observation = {
            "changed_paths": list(changed),
            "protected_paths": protected,
            "unexpected_paths": unexpected,
        }
        process = ProcessResult(
            command=("trusted-path-policy",),
            returncode=0 if not violations else 1,
            timed_out=False,
            duration_ms=0,
            stdout=json.dumps(observation, sort_keys=True),
            stderr="",
            output_truncated=False,
        )
        return CaseResult(
            generation=generation,
            repetition=repetition,
            case_id="candidate-path-policy",
            visibility="development",
            process=process,
            observation_hash=stable_hash(observation),
            oracle_passed=not violations,
            policy_passed=not violations,
            failure="policy" if violations else None,
        )

    def run_candidate_verification(
        self,
        candidate: Path,
        *,
        generation: str,
        repetition: int,
    ) -> CaseResult:
        """Run candidate-owned verification read-only in a networkless sandbox."""
        candidate = candidate.resolve()
        process_guard = self.trusted_root / "evaluation" / "process_guard.py"
        environment = (self.trusted_root / ".venv").resolve()
        if not environment.is_dir():
            raise EvaluationError("The trusted project virtualenv is unavailable.")
        site_packages = next(environment.glob("lib/python*/site-packages"), None)
        if site_packages is None:
            raise EvaluationError(
                "The trusted Python site-packages directory is missing."
            )
        hidden_mounts: list[str] = []
        for relative in ("evaluation/holdout", "evaluation/oracles"):
            if (candidate / relative).is_dir():
                hidden_mounts.extend(
                    ["--size", "4096", "--tmpfs", f"/workspace/{relative}"]
                )
        command = [
            self.bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(self.budget.max_disk_mb * 1024 * 1024),
            "--perms",
            "1777",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(candidate),
            "/workspace",
            *hidden_mounts,
            "--ro-bind",
            str(environment),
            "/environment",
            "--dir",
            "/trusted",
            "--ro-bind",
            str(process_guard),
            "/trusted/process_guard.py",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--setenv",
            "PYTHONPYCACHEPREFIX",
            "/tmp/pycache",
            "--setenv",
            "PYTHONPATH",
            f"/environment/{site_packages.relative_to(environment)}",
            "--setenv",
            "CANDIDATE_EVALUATION",
            "1",
            "--chdir",
            "/workspace",
            "/usr/bin/python3",
            "/trusted/process_guard.py",
            "--max-processes",
            str(self.budget.max_processes),
            "--",
            "/usr/bin/make",
            "verify",
            "PYTHON=/usr/bin/python3",
        ]
        process = self._run(command)
        passed = (
            not process.timed_out
            and not process.output_truncated
            and process.returncode == 0
        )
        if process.timed_out:
            failure = "timeout"
        elif process.output_truncated:
            failure = "output_budget"
        elif process.returncode != 0:
            failure = "verification"
        else:
            failure = None
        output_hash = stable_hash({"stdout": process.stdout, "stderr": process.stderr})
        return CaseResult(
            generation=generation,
            repetition=repetition,
            case_id="candidate-verification",
            visibility="development",
            process=process,
            observation_hash=output_hash,
            oracle_passed=passed,
            policy_passed=not process.timed_out and not process.output_truncated,
            failure=failure,
        )


def evaluate_pair(
    *,
    trusted_root: Path,
    baseline: Path,
    candidate: Path,
    development: SuiteManifest,
    holdout: SuiteManifest,
    holdout_oracle: dict[str, dict[str, Any]],
    holdout_oracle_hash: str,
    repetitions: int,
    budget: EvaluationBudget,
    allowed_candidate_paths: set[str],
    expected_environment_hash: str | None = None,
    state: StateStore | None = None,
    campaign_id: str | None = None,
    build_id: str | None = None,
) -> PairedEvaluation:
    """Evaluate clean baseline and candidate commits under one supervisor."""
    if repetitions < 1 or repetitions > 10:
        raise EvaluationError("Repetitions must be between 1 and 10.")
    if not allowed_candidate_paths:
        raise EvaluationError("At least one candidate path must be predeclared.")
    baseline_commit = committed_generation(baseline)
    candidate_commit = committed_generation(candidate)
    _, environment_hash = environment_identity(trusted_root)
    if (
        expected_environment_hash is not None
        and expected_environment_hash != environment_hash
    ):
        raise EvaluationError("Evaluator environment hash mismatch.")
    patch, patch_hash = candidate_patch(
        candidate,
        baseline_commit=baseline_commit,
        max_bytes=budget.max_output_bytes,
    )
    supervisor = Supervisor(trusted_root, budget)
    evaluation_id = None
    if state is not None:
        evaluation_id = state.create_evaluation(
            campaign_id=campaign_id,
            baseline_commit=baseline_commit,
            candidate_commit=candidate_commit,
            suite_id=f"{development.suite_id}+{holdout.suite_id}",
            suite_hash=stable_hash(
                {
                    "development": development.manifest_hash,
                    "holdout": holdout.manifest_hash,
                }
            ),
            holdout_hash=stable_hash(
                {
                    "manifest": holdout.manifest_hash,
                    "oracle": holdout_oracle_hash,
                }
            ),
            environment_hash=environment_hash,
            repetitions=repetitions,
            budget=asdict(budget),
            build_id=build_id,
        )
        state.add_evaluation_artifact(
            evaluation_id,
            kind="candidate_patch",
            content_hash=patch_hash,
            content=patch,
        )
    results: list[CaseResult] = []
    try:
        for generation, root in (("baseline", baseline), ("candidate", candidate)):
            for repetition in range(1, repetitions + 1):
                policy_allowed = (
                    allowed_candidate_paths if generation == "candidate" else set()
                )
                results.append(
                    supervisor.run_path_policy(
                        root,
                        baseline_commit=baseline_commit,
                        allowed_paths=policy_allowed,
                        generation=generation,
                        repetition=repetition,
                    )
                )
                if state is not None and evaluation_id is not None:
                    _persist_case(state, evaluation_id, results[-1])
                results.append(
                    supervisor.run_candidate_verification(
                        root,
                        generation=generation,
                        repetition=repetition,
                    )
                )
                if state is not None and evaluation_id is not None:
                    _persist_case(state, evaluation_id, results[-1])
                for case in development.cases:
                    if case.oracle is None:
                        raise EvaluationError("Development case is missing its oracle.")
                    results.append(
                        supervisor.run_contract(
                            root,
                            case,
                            generation=generation,
                            repetition=repetition,
                            visibility="development",
                            oracle=case.oracle,
                        )
                    )
                    if state is not None and evaluation_id is not None:
                        _persist_case(state, evaluation_id, results[-1])
                for case in holdout.cases:
                    results.append(
                        supervisor.run_contract(
                            root,
                            case,
                            generation=generation,
                            repetition=repetition,
                            visibility="holdout",
                            oracle=holdout_oracle[case.case_id],
                        )
                    )
                    if state is not None and evaluation_id is not None:
                        _persist_case(state, evaluation_id, results[-1])
    except EvaluationError as exc:
        if state is not None and evaluation_id is not None:
            status = "budget_exhausted" if "budget" in str(exc).lower() else "failed"
            state.complete_evaluation(
                evaluation_id,
                status=status,
                scorecard={"error": type(exc).__name__, "message": str(exc)},
            )
        raise
    return PairedEvaluation(
        baseline_commit=baseline_commit,
        candidate_commit=candidate_commit,
        development_suite_hash=development.manifest_hash,
        holdout_suite_hash=holdout.manifest_hash,
        holdout_oracle_hash=holdout_oracle_hash,
        environment_hash=environment_hash,
        candidate_patch_hash=patch_hash,
        repetitions=repetitions,
        budget=budget,
        results=tuple(results),
        evaluation_id=evaluation_id,
        build_id=build_id,
    )


def _persist_case(
    state: StateStore,
    evaluation_id: str,
    result: CaseResult,
) -> None:
    state.add_evaluation_case(
        evaluation_id,
        generation=result.generation,
        repetition=result.repetition,
        case_id=result.case_id,
        visibility=result.visibility,
        result=result.to_dict(redact_holdout=False),
    )
