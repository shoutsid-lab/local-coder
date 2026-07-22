#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
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
DEVELOPMENT_SUITE = ROOT / "evaluation" / "suites" / "atomic-v1.json"
HOLDOUT_STORAGE = ROOT / ".local-coder" / "holdout"
GEPA_DATASET_PATH = ROOT / ".local-coder" / "gepa-datasets" / "latest"


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
    dspy_ok = importlib.util.find_spec("dspy") is not None

    branch_status, branch = command_output(["git", "branch", "--show-current"])
    tree_status, porcelain = command_output(["git", "status", "--porcelain"])

    branch = branch if branch_status == 0 and branch else "unknown"
    tree_clean = tree_status == 0 and not porcelain

    print(f"llama-server :8080  {'OK' if llama_ok else 'UNAVAILABLE'}")
    print(f"LiteLLM      :4000  {'OK' if litellm_ok else 'UNAVAILABLE'}")
    print(f"smolagents          {'OK' if smolagents_ok else 'NOT INSTALLED'}")
    print(f"DSPy                {'OK' if dspy_ok else 'NOT INSTALLED'}")
    print(f"Python              {sys.executable}")
    print(f"Git branch          {branch}")
    print(f"Working tree        {'clean' if tree_clean else 'has changes'}")
    print(f"Run database        {STATE_PATH}")

    ready = llama_ok and litellm_ok and smolagents_ok and dspy_ok and branch_status == 0
    return 0 if ready else 1


def handle_repair(args: argparse.Namespace) -> int:
    return run_command(
        [
            sys.executable,
            "./run-editor.py",
            args.instruction,
            *args.files,
        ]
    )


def handle_verify(_: argparse.Namespace) -> int:
    return run_command(["make", "verify"])


def handle_review(args: argparse.Namespace) -> int:
    return run_command([sys.executable, "./review-diff.py", "--task", str(args.task)])


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
        expected_changed_paths=(
            tuple(args.expected_file) if args.expected_file else None
        ),
    )
    summary = AgentOrchestrator(config).run(args.task)
    print(summary.to_json())
    completed = {
        "awaiting_approval",
        "needs_attention",
        "no_changes",
    }
    return 0 if summary.status in completed else 1


def handle_validate_plan(args: argparse.Namespace) -> int:
    """Validate and hash one externally authored atomic task plan read-only."""
    from runtime.plans import PlanError, load_task_plan, plan_summary

    try:
        plan = load_task_plan(args.plan, repository=ROOT)
    except (OSError, PlanError) as exc:
        print(f"Task plan rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(plan_summary(plan), indent=2, sort_keys=True))
    return 0


def handle_run_plan_step(args: argparse.Namespace) -> int:
    """Run exactly one explicitly selected, hash-approved plan step."""
    from runtime.orchestrator import AgentOrchestrator, OrchestratorConfig
    from runtime.plans import PlanError, load_task_plan, render_step_task

    try:
        plan = load_task_plan(args.plan, repository=ROOT)
        if args.approve_plan_hash != plan.plan_hash:
            raise PlanError("Approved plan hash does not match the validated plan.")
        step = plan.step(args.step_id)
        step_index = next(
            index
            for index, candidate in enumerate(plan.steps)
            if candidate.id == step.id
        )
        prior_steps = {candidate.id for candidate in plan.steps[:step_index]}
        completed_steps = set(args.completed_step or ())
        invalid_completed = completed_steps - prior_steps
        if invalid_completed:
            invalid = ", ".join(sorted(invalid_completed))
            raise PlanError(f"Completed steps are not prior plan steps: {invalid}")
        missing_dependencies = set(step.depends_on) - completed_steps
        if missing_dependencies:
            missing = ", ".join(sorted(missing_dependencies))
            raise PlanError(f"Step dependencies are not completed: {missing}")
    except (OSError, PlanError) as exc:
        print(f"Task plan rejected: {exc}", file=sys.stderr)
        return 1

    config = OrchestratorConfig(
        repository=ROOT,
        max_steps=args.max_steps,
        keep_worktree=True,
        mode="agentic",
        expected_changed_paths=step.editable_files,
    )
    summary = AgentOrchestrator(config).run(render_step_task(plan, step))
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


def handle_analyze_runs(args: argparse.Namespace) -> int:
    """Normalize recorded runs without mutating their SQLite database."""
    from evaluation.outcomes import analyze_run_records
    from runtime.state import StateStore

    try:
        store = StateStore(args.database.resolve(), read_only=True)
    except FileNotFoundError:
        print(f"Run database does not exist: {args.database}", file=sys.stderr)
        return 1
    run_ids = args.run_id or [row["id"] for row in store.recent_runs(args.limit)]
    records = []
    for run_id in run_ids:
        details = store.run_details(run_id)
        if details is None:
            print(f"Unknown run ID: {run_id}", file=sys.stderr)
            return 1
        records.append(details)
    print(json.dumps(analyze_run_records(records), indent=2, sort_keys=True))
    return 0


def handle_export_gepa_dataset(args: argparse.Namespace) -> int:
    """Export complete typed DSPy traces without mutating audit state."""
    from runtime.dspy_programs.gepa_dataset import (
        GepaDatasetError,
        export_gepa_dataset,
    )

    try:
        manifest = export_gepa_dataset(
            args.database,
            args.output,
            run_ids=args.run_id,
            limit=args.limit,
        )
    except GepaDatasetError as exc:
        print(f"GEPA dataset export failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _evaluation_budget(args: argparse.Namespace):
    from evaluation.supervisor import EvaluationBudget

    return EvaluationBudget(
        campaign_wall_seconds=args.campaign_seconds,
        process_wall_seconds=args.process_seconds,
        max_processes=args.max_processes,
        max_output_bytes=args.max_output_bytes,
        max_memory_mb=args.max_memory_mb,
        max_file_mb=args.max_file_mb,
        max_disk_mb=args.max_disk_mb,
        max_prompt_tokens=args.max_prompt_tokens,
        max_completion_tokens=args.max_completion_tokens,
        max_model_calls=args.max_model_calls,
    )


def _is_external_holdout(path: Path) -> bool:
    """Return whether a secret path is outside candidate-visible Git content."""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(ROOT)
    except ValueError:
        return True
    return relative.parts[:2] == (".local-coder", "holdout")


def _load_evaluation_inputs(
    args: argparse.Namespace,
    *,
    require_external_holdout: bool,
):
    from evaluation.manifests import load_holdout_oracle, load_suite

    if require_external_holdout and not all(
        _is_external_holdout(path) for path in (args.holdout_suite, args.holdout_oracle)
    ):
        raise ValueError(
            "Campaign holdout inputs must be outside candidate-visible Git content."
        )

    development = load_suite(
        args.development_suite,
        expected_visibility="development",
    )
    holdout = load_suite(args.holdout_suite, expected_visibility="holdout")
    oracle, oracle_hash = load_holdout_oracle(args.holdout_oracle, holdout)
    return development, holdout, oracle, oracle_hash


def handle_evaluate(args: argparse.Namespace) -> int:
    """Run and record one bounded paired generation evaluation."""
    from dataclasses import asdict

    from evaluation.outcomes import candidate_trajectory_evidence, stable_hash
    from evaluation.scorecard import build_scorecard
    from evaluation.supervisor import EvaluationError, evaluate_pair
    from runtime.state import StateStore

    try:
        development, holdout, oracle, oracle_hash = _load_evaluation_inputs(
            args,
            require_external_holdout=args.campaign_id is not None,
        )
        budget = _evaluation_budget(args)
        state = StateStore(args.database.resolve())
        trajectory = None
        if args.campaign_id is not None:
            if args.build_id is None:
                raise ValueError("Campaign evaluation requires --build-id.")
            campaign = state.campaign_details(args.campaign_id)
            approved = (
                [brief for brief in campaign["briefs"] if brief["status"] == "approved"]
                if campaign is not None
                else []
            )
            if len(approved) != 1:
                raise ValueError("Campaign requires one approved brief.")
            holdout_hash = stable_hash(
                {
                    "manifest": holdout.manifest_hash,
                    "oracle": oracle_hash,
                }
            )
            if campaign.get("holdout_hash") != holdout_hash:
                raise ValueError("Holdout identity does not match the campaign.")
            campaign_environment_hash = campaign.get("environment_hash")
            if not campaign_environment_hash:
                raise ValueError("Campaign has no frozen evaluator environment.")
            if (
                args.expected_environment_hash is not None
                and args.expected_environment_hash != campaign_environment_hash
            ):
                raise ValueError(
                    "CLI environment hash does not match the campaign identity."
                )
            allowed_candidate_paths = set(json.loads(approved[0]["allowed_files"]))
            acceptance_cases = {
                metric["case_id"]
                for metric in json.loads(approved[0]["acceptance_metrics"])
            }
            if set(args.target_case) != acceptance_cases:
                raise ValueError("Target cases do not match the approved brief.")
            if args.allowed_file and set(args.allowed_file) != allowed_candidate_paths:
                raise ValueError("CLI allowed paths do not match the approved brief.")
            build = state.candidate_build_details(args.build_id)
            if build is None or build["campaign_id"] != args.campaign_id:
                raise ValueError("Candidate build does not belong to the campaign.")
            if Path(build["worktree"] or "").resolve() != args.candidate.resolve():
                raise ValueError(
                    "Candidate path differs from the recorded build worktree."
                )
            trajectory = candidate_trajectory_evidence(build, asdict(budget))
        else:
            if args.build_id is not None:
                raise ValueError("Standalone evaluation cannot name a campaign build.")
            if not args.allowed_file:
                raise ValueError("Standalone evaluation requires --allowed-file.")
            allowed_candidate_paths = set(args.allowed_file)
            campaign_environment_hash = args.expected_environment_hash
        evaluation = evaluate_pair(
            trusted_root=ROOT,
            baseline=args.baseline,
            candidate=args.candidate,
            development=development,
            holdout=holdout,
            holdout_oracle=oracle,
            holdout_oracle_hash=oracle_hash,
            repetitions=args.repetitions,
            budget=budget,
            allowed_candidate_paths=allowed_candidate_paths,
            expected_environment_hash=campaign_environment_hash,
            state=state,
            campaign_id=args.campaign_id,
            build_id=args.build_id,
            trajectory_evidence=trajectory,
        )
        scorecard = build_scorecard(
            evaluation,
            target_case_ids=args.target_case,
            trajectory_evidence=trajectory,
        )
        if evaluation.evaluation_id is not None:
            state.complete_evaluation(
                evaluation.evaluation_id,
                status="completed",
                scorecard=scorecard.to_dict(),
            )
    except (EvaluationError, ValueError) as exc:
        print(f"Evaluation failed closed: {exc}", file=sys.stderr)
        return 1
    report = evaluation.to_dict(redact_holdout=True)
    report["scorecard"] = scorecard.to_dict()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if scorecard.recommendation == "eligible_for_promotion" else 2


def handle_rotate_holdout(args: argparse.Namespace) -> int:
    """Provision one immutable holdout rotation outside candidate Git history."""
    from evaluation.manifests import load_holdout_oracle, load_suite

    rotation_id = args.rotation_id.strip()
    if (
        not rotation_id
        or len(rotation_id) > 80
        or rotation_id.startswith(".")
        or any(
            character not in "-_.abcdefghijklmnopqrstuvwxyz0123456789"
            for character in rotation_id.lower()
        )
    ):
        print("Holdout rotation failed: invalid rotation ID.", file=sys.stderr)
        return 1
    manifest_source = args.manifest.resolve()
    oracle_source = args.oracle.resolve()
    if not all(_is_external_holdout(path) for path in (manifest_source, oracle_source)):
        print(
            "Holdout rotation failed: sources must not be candidate-visible files.",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = load_suite(manifest_source, expected_visibility="holdout")
        load_holdout_oracle(oracle_source, manifest)
        HOLDOUT_STORAGE.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = HOLDOUT_STORAGE / rotation_id
        if destination.exists():
            raise ValueError("Rotation ID already exists and is immutable.")
        staging = Path(tempfile.mkdtemp(prefix=".rotation-", dir=HOLDOUT_STORAGE))
        try:
            manifest_target = staging / "manifest.json"
            oracle_target = staging / "oracle.json"
            shutil.copyfile(manifest_source, manifest_target)
            shutil.copyfile(oracle_source, oracle_target)
            manifest_target.chmod(0o600)
            oracle_target.chmod(0o600)
            staging.rename(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except (OSError, ValueError) as exc:
        print(f"Holdout rotation failed closed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "rotation_id": rotation_id,
                "holdout_suite": str(destination / "manifest.json"),
                "holdout_oracle": str(destination / "oracle.json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def handle_create_campaign(args: argparse.Namespace) -> int:
    """Mine one failure class and create one pending campaign brief."""
    from dataclasses import asdict

    from evaluation.miner import campaign_candidate_limit, mine_improvement_brief
    from evaluation.outcomes import normalize_run, stable_hash
    from evaluation.supervisor import (
        EvaluationError,
        committed_generation,
        environment_identity,
    )
    from runtime.editor import PIPELINE_CONTROLS
    from runtime.state import StateStore

    try:
        store = StateStore(args.database.resolve())
        records = []
        for run_id in args.run_id:
            details = store.run_details(run_id)
            if details is None:
                raise ValueError(f"Unknown run ID: {run_id}")
            records.append(normalize_run(details))
        development, holdout, _, oracle_hash = _load_evaluation_inputs(
            args,
            require_external_holdout=True,
        )
        suite_hash = stable_hash(
            {
                "development": development.manifest_hash,
                "holdout": holdout.manifest_hash,
            }
        )
        budget_value = _evaluation_budget(args)
        budget_value.validate()
        budget = asdict(budget_value)
        holdout_hash = stable_hash(
            {"manifest": holdout.manifest_hash, "oracle": oracle_hash}
        )
        _, evaluator_environment_hash = environment_identity(ROOT)
        forbidden = set(PIPELINE_CONTROLS) | {
            "evaluation/",
            "tests/test_architecture_contract.py",
            "tests/test_evaluation_contract.py",
        }
        metrics = tuple(
            {
                "case_id": case_id,
                "measure": "paired_pass_count",
                "direction": "increase",
            }
            for case_id in args.target_case
        )
        baseline_commit = committed_generation(args.baseline)
        brief = mine_improvement_brief(
            records,
            baseline_commit=baseline_commit,
            allowed_files=args.allowed_file,
            forbidden_files=forbidden,
            acceptance_metrics=metrics,
            suite_hash=suite_hash,
            budget=budget,
            rollback_condition=args.rollback_condition,
        )
        campaign_id = store.create_campaign(
            baseline_commit=baseline_commit,
            suite_hash=suite_hash,
            budget=budget,
            max_candidates=campaign_candidate_limit(store),
            holdout_hash=holdout_hash,
            environment_hash=evaluator_environment_hash,
        )
        store.add_improvement_brief(campaign_id, brief.to_dict())
    except (EvaluationError, ValueError) as exc:
        print(f"Campaign creation failed closed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"campaign_id": campaign_id, "brief": brief.to_dict()},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def handle_approve_brief(args: argparse.Namespace) -> int:
    """Record explicit actor approval of a pending improvement brief."""
    from runtime.state import StateStore

    try:
        StateStore(args.database.resolve()).approve_improvement_brief(
            args.brief_id,
            actor=args.actor,
            rationale=args.rationale,
        )
    except ValueError as exc:
        print(f"Brief approval failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"brief_id": args.brief_id, "status": "approved"}))
    return 0


def handle_build_candidate(args: argparse.Namespace) -> int:
    """Build one uncommitted campaign candidate through the existing orchestrator."""
    from evaluation.miner import ExperimentOverlay
    from runtime.orchestrator import AgentOrchestrator, OrchestratorConfig
    from runtime.state import StateStore

    store = StateStore(STATE_PATH)
    campaign = store.campaign_details(args.campaign_id)
    if campaign is None or campaign["status"] != "active":
        print(
            "Candidate build failed: campaign is missing or inactive.", file=sys.stderr
        )
        return 1
    briefs = [brief for brief in campaign["briefs"] if brief["status"] == "approved"]
    if len(briefs) != 1:
        print(
            "Candidate build failed: one approved brief is required.", file=sys.stderr
        )
        return 1
    brief = briefs[0]
    try:
        overlay_values = dict(item.split("=", 1) for item in (args.overlay or []))
        overlay = (
            ExperimentOverlay.from_mapping(overlay_values) if overlay_values else None
        )
        allowed_files = json.loads(brief["allowed_files"])
        acceptance_metrics = json.loads(brief["acceptance_metrics"])
        brief_budget = json.loads(brief["budget"])
        forbidden_files = json.loads(brief["forbidden_files"])
        build_id = store.create_candidate_build(
            args.campaign_id,
            brief_id=brief["id"],
            overlay_hash=overlay.overlay_hash if overlay is not None else None,
            overlay=dict(overlay.values) if overlay is not None else None,
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Candidate build failed closed: {exc}", file=sys.stderr)
        return 1
    task = "\n".join(
        (
            f"Implement approved recursive-improvement brief {brief['id']}.",
            f"Failure class: {brief['failure_class']}",
            f"Falsifiable hypothesis: {brief['hypothesis']}",
            f"Editable files exactly: {json.dumps(allowed_files)}",
            f"Forbidden files: {json.dumps(forbidden_files)}",
            f"Acceptance metrics: {json.dumps(acceptance_metrics, sort_keys=True)}",
            f"Rollback condition: {brief['rollback_condition']}",
            (
                f"In-memory overlay identity: {overlay.overlay_hash}"
                if overlay is not None
                else "No in-memory overlay."
            ),
            "Leave all changes uncommitted for independent inspection.",
        )
    )
    try:
        summary = AgentOrchestrator(
            OrchestratorConfig(
                repository=ROOT,
                max_steps=min(args.max_steps, brief_budget["max_model_calls"]),
                keep_worktree=True,
                mode="agentic",
                expected_changed_paths=tuple(allowed_files),
                max_model_calls=brief_budget["max_model_calls"],
                max_prompt_tokens=brief_budget["max_prompt_tokens"],
                max_completion_tokens=brief_budget["max_completion_tokens"],
            )
        ).run(task)
        store.complete_candidate_build(
            build_id,
            run_id=summary.run_id,
            status=summary.status,
            branch=summary.branch,
            worktree=summary.worktree,
        )
    except Exception as exc:
        store.complete_candidate_build(
            build_id,
            run_id=None,
            status="failed",
            branch=None,
            worktree=None,
        )
        print(f"Candidate build failed closed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"build_id": build_id, "run": json.loads(summary.to_json())},
            indent=2,
            sort_keys=True,
        )
    )
    return (
        0
        if summary.status in {"awaiting_approval", "needs_attention", "no_changes"}
        else 1
    )


def handle_record_decision(args: argparse.Namespace) -> int:
    """Record an authorization decision without changing Git state."""
    from runtime.state import StateStore

    try:
        StateStore(args.database.resolve()).record_promotion_decision(
            args.evaluation_id,
            actor=args.actor,
            decision=args.decision,
            rationale=args.rationale,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        print(f"Decision recording failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"evaluation_id": args.evaluation_id, "decision": args.decision}))
    return 0


def handle_close_campaign(args: argparse.Namespace) -> int:
    """Close a decided campaign from its stored safety and regression evidence."""
    from runtime.state import StateStore

    try:
        status = StateStore(args.database.resolve()).close_campaign_from_evidence(
            args.campaign_id
        )
    except ValueError as exc:
        print(f"Campaign close failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"campaign_id": args.campaign_id, "status": status}))
    return 0


def handle_show_campaign(args: argparse.Namespace) -> int:
    """Show persisted campaign lineage and decisions."""
    from runtime.state import StateStore

    details = StateStore(args.database.resolve(), read_only=True).campaign_details(
        args.campaign_id
    )
    if details is None:
        print(f"Unknown campaign ID: {args.campaign_id}", file=sys.stderr)
        return 1
    print(json.dumps(details, indent=2, sort_keys=True))
    return 0


def handle_audit_campaign(args: argparse.Namespace) -> int:
    """Audit completed campaign lineage without mutating state or Git."""
    from evaluation.audit import audit_campaign
    from runtime.state import StateStore

    try:
        store = StateStore(args.database.resolve(), read_only=True)
    except FileNotFoundError:
        print(f"Run database does not exist: {args.database}", file=sys.stderr)
        return 1
    report = audit_campaign(store, args.campaign_id)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 2


def add_evaluation_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared immutable-suite and hard-budget arguments."""
    parser.add_argument("--development-suite", type=Path, default=DEVELOPMENT_SUITE)
    parser.add_argument("--holdout-suite", type=Path, required=True)
    parser.add_argument("--holdout-oracle", type=Path, required=True)
    parser.add_argument("--campaign-seconds", type=int, default=1800)
    parser.add_argument("--process-seconds", type=int, default=180)
    parser.add_argument("--max-processes", type=int, default=64)
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-memory-mb", type=int, default=2048)
    parser.add_argument("--max-file-mb", type=int, default=64)
    parser.add_argument("--max-disk-mb", type=int, default=512)
    parser.add_argument("--max-prompt-tokens", type=int, default=200_000)
    parser.add_argument("--max-completion-tokens", type=int, default=100_000)
    parser.add_argument("--max-model-calls", type=int, default=64)


def handle_skills(_: argparse.Namespace) -> int:
    from runtime.skills import runtime_skill_config
    from runtime.skills_loader import discover_skills

    skills = discover_skills(ROOT / ".local-coder" / "skills")
    payload = []
    for skill in skills.values():
        config = runtime_skill_config(skill.name)
        payload.append(
            {
                "name": skill.name,
                "description": skill.description,
                "model": config.model,
                "tools": list(config.tools),
                "max_steps": config.max_steps,
            }
        )
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
    run_parser.add_argument(
        "--expected-file",
        action="append",
        help="Predeclare an expected changed path; may be repeated.",
    )
    run_parser.set_defaults(handler=handle_run)

    validate_plan_parser = subparsers.add_parser(
        "validate-plan",
        help="Validate and hash an externally authored atomic task plan read-only.",
    )
    validate_plan_parser.add_argument("plan", type=Path)
    validate_plan_parser.set_defaults(handler=handle_validate_plan)

    run_plan_parser = subparsers.add_parser(
        "run-plan-step",
        help="Run one explicitly selected, hash-approved atomic plan step.",
    )
    run_plan_parser.add_argument("plan", type=Path)
    run_plan_parser.add_argument("step_id")
    run_plan_parser.add_argument(
        "--approve-plan-hash",
        required=True,
        help="Exact SHA-256 emitted by validate-plan.",
    )
    run_plan_parser.add_argument(
        "--completed-step",
        action="append",
        help="Attest one completed prior dependency; may be repeated.",
    )
    run_plan_parser.add_argument(
        "--max-steps",
        type=int,
        default=8,
        help="Maximum manager-agent steps for this atomic plan step.",
    )
    run_plan_parser.set_defaults(handler=handle_run_plan_step)

    runs_parser = subparsers.add_parser("runs", help="List recent audited runs.")
    runs_parser.add_argument("--limit", type=int, default=20)
    runs_parser.set_defaults(handler=handle_runs)

    show_parser = subparsers.add_parser("show-run", help="Show one audited run.")
    show_parser.add_argument("run_id")
    show_parser.set_defaults(handler=handle_show_run)

    analyze_parser = subparsers.add_parser(
        "analyze-runs",
        help="Normalize historical run outcomes without model services or writes.",
    )
    analyze_parser.add_argument(
        "--database",
        type=Path,
        default=STATE_PATH,
        help="SQLite audit database to inspect read-only.",
    )
    analyze_parser.add_argument("--limit", type=int, default=20)
    analyze_parser.add_argument(
        "--run-id",
        action="append",
        help="Specific run ID to analyze; may be repeated.",
    )
    analyze_parser.set_defaults(handler=handle_analyze_runs)

    gepa_export_parser = subparsers.add_parser(
        "export-gepa-dataset",
        help="Export complete audited DSPy traces for offline GEPA work.",
    )
    gepa_export_parser.add_argument(
        "--database",
        type=Path,
        default=STATE_PATH,
        help="SQLite audit database to inspect read-only.",
    )
    gepa_export_parser.add_argument(
        "--output",
        type=Path,
        default=GEPA_DATASET_PATH,
        help="Destination directory for the manifest and JSONL splits.",
    )
    gepa_export_parser.add_argument("--limit", type=int, default=100)
    gepa_export_parser.add_argument(
        "--run-id",
        action="append",
        help="Specific run ID to export; may be repeated.",
    )
    gepa_export_parser.set_defaults(handler=handle_export_gepa_dataset)

    rotate_parser = subparsers.add_parser(
        "rotate-holdout",
        help="Provision immutable external holdout inputs for a campaign.",
    )
    rotate_parser.add_argument("rotation_id")
    rotate_parser.add_argument("--manifest", type=Path, required=True)
    rotate_parser.add_argument("--oracle", type=Path, required=True)
    rotate_parser.set_defaults(handler=handle_rotate_holdout)

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Run a bounded trusted baseline/candidate comparison.",
    )
    evaluate_parser.add_argument("--baseline", type=Path, required=True)
    evaluate_parser.add_argument("--candidate", type=Path, required=True)
    evaluate_parser.add_argument("--database", type=Path, default=STATE_PATH)
    evaluate_parser.add_argument("--campaign-id")
    evaluate_parser.add_argument(
        "--build-id",
        help="Exact audited candidate build; required with --campaign-id.",
    )
    evaluate_parser.add_argument(
        "--allowed-file",
        action="append",
        help="Predeclared candidate path; required without a campaign.",
    )
    evaluate_parser.add_argument("--repetitions", type=int, default=1)
    evaluate_parser.add_argument("--expected-environment-hash")
    evaluate_parser.add_argument(
        "--target-case",
        action="append",
        required=True,
        help="Predeclared case whose paired pass count must improve.",
    )
    add_evaluation_arguments(evaluate_parser)
    evaluate_parser.set_defaults(handler=handle_evaluate)

    campaign_parser = subparsers.add_parser(
        "create-campaign",
        help="Create one bounded campaign and one pending improvement brief.",
    )
    campaign_parser.add_argument("--baseline", type=Path, required=True)
    campaign_parser.add_argument("--database", type=Path, default=STATE_PATH)
    campaign_parser.add_argument("--run-id", action="append", required=True)
    campaign_parser.add_argument("--allowed-file", action="append", required=True)
    campaign_parser.add_argument("--target-case", action="append", required=True)
    campaign_parser.add_argument("--rollback-condition", required=True)
    add_evaluation_arguments(campaign_parser)
    campaign_parser.set_defaults(handler=handle_create_campaign)

    approve_parser = subparsers.add_parser(
        "approve-brief",
        help="Record explicit actor approval for one campaign brief.",
    )
    approve_parser.add_argument("brief_id")
    approve_parser.add_argument("--actor", required=True)
    approve_parser.add_argument("--rationale", required=True)
    approve_parser.add_argument("--database", type=Path, default=STATE_PATH)
    approve_parser.set_defaults(handler=handle_approve_brief)

    build_candidate_parser = subparsers.add_parser(
        "build-candidate",
        help="Build one approved candidate and leave its worktree uncommitted.",
    )
    build_candidate_parser.add_argument("campaign_id")
    build_candidate_parser.add_argument("--max-steps", type=int, default=12)
    build_candidate_parser.add_argument(
        "--overlay",
        action="append",
        help="Allowlisted in-memory KEY=VALUE prompt/skill variant.",
    )
    build_candidate_parser.set_defaults(handler=handle_build_candidate)

    decision_parser = subparsers.add_parser(
        "record-decision",
        help="Record a promotion decision without changing Git.",
    )
    decision_parser.add_argument("evaluation_id")
    decision_parser.add_argument("decision", choices=("promote", "reject"))
    decision_parser.add_argument("--actor", required=True)
    decision_parser.add_argument("--rationale", required=True)
    decision_parser.add_argument("--database", type=Path, default=STATE_PATH)
    decision_parser.set_defaults(handler=handle_record_decision)

    close_parser = subparsers.add_parser(
        "close-campaign",
        help="Close a decided campaign from stored gate evidence.",
    )
    close_parser.add_argument("campaign_id")
    close_parser.add_argument("--database", type=Path, default=STATE_PATH)
    close_parser.set_defaults(handler=handle_close_campaign)

    show_campaign_parser = subparsers.add_parser(
        "show-campaign",
        help="Show campaign lineage, briefs, evaluations, and decisions.",
    )
    show_campaign_parser.add_argument("campaign_id")
    show_campaign_parser.add_argument("--database", type=Path, default=STATE_PATH)
    show_campaign_parser.set_defaults(handler=handle_show_campaign)

    audit_campaign_parser = subparsers.add_parser(
        "audit-campaign",
        help="Audit completed recursive-improvement campaign invariants read-only.",
    )
    audit_campaign_parser.add_argument("campaign_id")
    audit_campaign_parser.add_argument("--database", type=Path, default=STATE_PATH)
    audit_campaign_parser.set_defaults(handler=handle_audit_campaign)

    skills_parser = subparsers.add_parser("skills", help="List loaded agent skills.")
    skills_parser.set_defaults(handler=handle_skills)

    repair_parser = subparsers.add_parser(
        "repair", help="Apply one validated native atomic edit."
    )
    repair_parser.add_argument("instruction", help="Exact atomic editing instruction.")
    repair_parser.add_argument("files", nargs="+", help="Approved files to edit.")
    repair_parser.set_defaults(handler=handle_repair)

    verify_parser = subparsers.add_parser(
        "verify", help="Run deterministic verification."
    )
    verify_parser.set_defaults(handler=handle_verify)

    review_parser = subparsers.add_parser("review", help="Review the current Git diff.")
    review_parser.add_argument(
        "task",
        type=Path,
        help="Authoritative task file for the diff under review.",
    )
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
