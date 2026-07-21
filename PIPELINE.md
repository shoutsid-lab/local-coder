# Local AI Coding Pipeline

## Purpose

This repository validates a hardware-adjusted local AI coding pipeline for small, controlled software changes.

The pipeline prioritises deterministic verification and human approval over autonomous model behaviour.

## Hardware Profile

* System RAM: 8 GiB
* GPU: NVIDIA GeForce GTX 1660 Ti
* GPU memory: 6 GiB
* CPU: Intel Core i7-10750H
* Environment: WSL2 Ubuntu
* Inference runtime: llama.cpp with CUDA acceleration

## Model Profile

* Model: Qwen2.5-Coder-3B-Instruct
* Quantisation: Q4_K_M
* API alias: `local-coder`
* Server endpoint: `http://127.0.0.1:8080/v1`
* Server context: 32,768 tokens
* Parallel slots: 1

## Coding Agent

Aider is used as the editing harness.

Normal task profile:

* Repository map enabled
* Repository-map budget: 2,048 tokens
* Chat-history summarisation threshold: 8,192 tokens
* Only relevant files are editable
* `CONVENTIONS.md` and `TASK.md` are read-only
* Automatic commits are disabled

Atomic repair profile:

* Fresh Aider process
* Repository map disabled
* One editable file where practical
* One explicit transformation per invocation
* Independent verification runs before and after the edit
* Changes remain uncommitted for human review

## Verification Pipeline

The authoritative verification command is:

```bash
make verify
```

It performs:

1. Black formatting validation
2. Flake8 lint validation
3. Pytest execution
4. Protected contract-test execution
5. Git whitespace validation

The model does not decide whether an edit is correct. The independent verification pipeline decides.

## Protected Files

The following files are not editable during implementation repairs:

* `test_pipeline_contract.py`
* `CONVENTIONS.md`
* `TASK.md`

Additional test files may also be treated as protected when only implementation code should change.

## Execution Flow

```text
Task request
    ↓
Human or planner creates an atomic instruction
    ↓
Aider sends the instruction to the local model
    ↓
The model edits explicitly allowed files
    ↓
make verify
    ├── pass → human reviews the diff
    └── fail → restore or issue another atomic instruction
    ↓
Human commits the verified change
```

## Model Capability Boundary

The current 3B model is suitable for:

* Explicit local transformations
* One-file bug fixes
* Small, tightly bounded edits
* Boilerplate generation
* Simple test additions
* Whole-file edits on small files

The current model is unreliable for:

* Diagnosing long or noisy failure logs
* Independently decomposing broad engineering tasks
* Multi-step semantic repair without external guidance
* Repeated autonomous self-correction
* Modifying many files in one request

Broader tasks must therefore be decomposed into atomic operations before execution.

## Git Policy

* Start tasks from a clean working tree.
* Make model changes on a dedicated branch or worktree.
* Disable automatic Aider commits.
* Review `git diff` after verification.
* Commit only after `make verify` passes.
* Restore failed edits before retrying with a new instruction.

## Context Benchmark

The context benchmark is separate from routine pytest discovery.

Run it only deliberately:

```bash
make context-benchmark
```

It must not run as part of:

```bash
make verify
```

## Current Validated Capabilities

* CUDA inference: passed
* OpenAI-compatible local API: passed
* 32K server context: passed
* Aider connectivity: passed
* Whole-file editing: passed
* Repository context: passed
* Protected tests: passed
* Formatting and lint gates: passed
* Independent verification: passed
* Atomic repair mode: passed
* Manual commit gate: passed

## Role-Separated Agent Runtime

The primary runtime is now a smolagents manager with managed explorer, planner,
implementer, repairer, and reviewer agents. It composes the existing Aider, LiteLLM,
worktree, verification, and review components rather than replacing them.

```bash
make agent-install
./local-coder.py run "Implement one concrete task"
```

The command requires a clean base repository, creates an isolated sibling worktree, and
leaves all edits uncommitted for human inspection. Each non-interactive Aider call applies
one atomic step; the orchestrator runs full deterministic verification after the planned
steps and invokes the read-only reviewer against tracked and untracked changes.

Run metadata and tool trajectories are written to `.local-coder/state/agent.db`. These
files are ignored and must not be committed.

## Codex Maintenance

Codex must follow `AGENTS.md` and use `HANDOFF.md` as the current-state brief. The
architecture is fixed unless the user explicitly changes it. `make verify` is the routine
gate; `make handoff-check` is the final clean-tree handoff gate.
