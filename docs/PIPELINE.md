# Local AI Coding Pipeline

## Purpose

This repository validates a hardware-adjusted local AI coding pipeline for small, controlled software changes.

The pipeline prioritises deterministic verification and explicit approval over
autonomous model behaviour.

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

## Native Atomic Editor

`runtime/editor.py` is the only source-editing worker.

Normal task profile:

* One or two approved existing files where practical
* Strict JSON search/replace operations through `local-fast`
* Exact old text must match once
* All paths and edits validate in memory before writes
* Protected controls and `*_contract.py` files are rejected
* No staging, commits, renames, file creation, or file deletion

Atomic repair profile:

* Fresh structured editor request
* One editable file where practical
* One explicit transformation per invocation
* Independent verification runs before and after the edit
* Changes remain uncommitted for independent review

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

* `tests/test_architecture_contract.py`
* `.flake8`
* `AGENTS.md`
* `ROADMAP.md`
* `docs/HANDOFF.md`
* `docs/ARCHITECTURE.md`
* `docs/PIPELINE.md`
* `docs/RECURSIVE_IMPROVEMENT.md`
* `docs/CONVENTIONS.md`
* `docs/VALIDATION_HISTORY.md`
* `pytest.ini`
* `requirements-agent.txt`
* `TASK.md`

Additional test files may also be treated as protected when only implementation code should change.

## Execution Flow

```text
Task request
    ↓
Requesting actor or planner creates an atomic instruction
    ↓
The native editor sends approved contents and a strict schema to `local-fast`
    ↓
The runtime validates and applies exact replacements to approved files
    ↓
make verify
    ├── pass → independent actor reviews the diff
    └── fail → restore or issue another atomic instruction
    ↓
Authorized actor commits the verified change
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

The primary capability experiment is now an optional reasoning-capable planner/reviewer
route evaluated on real repository tasks. Prompt optimization is retained, but it is not
assumed to overcome base-model reasoning limits and is not the current investment
priority.

All raw model-response boundaries use `runtime/model_response.py` to keep final content,
reasoning metadata, tool calls, finish reasons, and token usage separate. Reasoning text
is never substituted for a missing final answer or retained in normal audit records.

## Git Policy

* Start tasks from a clean working tree.
* Make model changes on a dedicated branch or worktree.
* The editor never stages or commits.
* Review `git diff` after verification.
* Commit only after `make verify` passes.
* Restore failed edits before retrying with a new instruction.

## Current Validated Capabilities

* CUDA inference: passed
* OpenAI-compatible local API: passed
* 32K server context: passed
* Native structured editor connectivity: passed
* Exact search/replace editing: passed
* Repository context: passed
* Protected tests: passed
* Formatting and lint gates: passed
* Independent verification: passed
* Atomic repair mode: passed
* Manual commit gate: passed

## Role-Separated Agent Runtime

The primary runtime is now a smolagents manager with managed explorer, planner,
implementer, repairer, and reviewer agents. It composes the native editor, LiteLLM,
worktree, verification, and review components.

```bash
make agent-install
./local-coder.py run "Implement one concrete task"
```

The command requires a clean base repository, creates an isolated sibling worktree, and
leaves all edits uncommitted for independent inspection. Each editor call validates a
complete atomic edit batch before writing; the orchestrator runs full deterministic
verification after the planned steps and invokes the read-only reviewer against tracked,
staged, and untracked changes.

Run metadata and tool trajectories are written to `.local-coder/state/agent.db`. These
files are ignored and must not be committed.

## Primary Actor Maintenance

The primary actor must follow `AGENTS.md` and use `ROADMAP.md` as the active work queue.
`docs/HISTORY.md` indexes completed programmes; detailed completion records are consulted
only when a task touches those subsystems. The architecture is fixed unless an authorized
actor explicitly changes it. `make verify` is the routine gate; `make handoff-check` is
the final clean-tree handoff gate. Improvement work must preserve the trusted evaluator,
holdout, decision, and deployment boundaries.

## Recursive Improvement Pipeline

The full operator procedure is documented in `docs/RECURSIVE_IMPROVEMENT.md`. Its hard
boundaries are:

1. `analyze-runs` opens SQLite read-only and emits hashes and structured facts, not raw
   untrusted prompts.
2. `create-campaign` mines exactly one failure class and records one pending brief.
3. An authorized actor approves the brief before an evaluation can join the campaign.
4. Baseline and candidate must be clean commits and run sequentially under the same
   environment hash and immutable suite hashes.
5. Candidate-owned verification cannot replace base-owned contracts or holdout oracles.
6. A scorecard can only recommend promotion; an authorized actor separately records the
   decision and performs any Git action outside the evaluator. The actor may be a trusted
   service or more capable model, but not the candidate under evaluation.

## Reasoning-aware route probes

Exact route health checks are not reasoning benchmarks. They send llama.cpp template
controls through LiteLLM with thinking disabled, a zero thinking budget, and a bounded
64-token final allowance. Run one independently with:

```bash
make route-probe ROUTE=local-fast MODE=exact
```

A separate capability probe enables bounded reasoning and succeeds only when the provider
returns observable `reasoning_content` followed by the exact final answer. It never copies
reasoning into final content or stores the full trace:

```bash
make route-probe ROUTE=local-reason MODE=reasoning
```

The live E2E continues probing `local-fast`, `local-plan`, and `local-review` in exact mode.
Set `LIVE_E2E_REASONING_ROUTE=<alias>` to add the optional reasoning probe without changing
the default route set. A reasoning-only response stopped by `finish_reason=length` fails as
`reasoning_only_truncated`; if that occurs during an exact probe, verify the model template
honors the per-request controls before increasing its token ceiling.

## Route-specific generation profiles

Generation policy is defined in `runtime/route_profiles.py`; it is not inferred from a
role name or applied as one global default. The existing `local-fast`, `local-plan`, and
`local-review` aliases remain assigned to the current local coding model with reasoning
disabled. The optional `local-reason` alias is additive and requires an operator-managed
model switch before use.

| Profile | Reasoning | Total completion | Reasoning / final | Purpose |
|---|---:|---:|---:|---|
| `local-fast` | off | 2048 | 0 / 2048 | implementation and repair |
| `local-plan` | off | 3072 | 0 / 3072 | current explorer and planner |
| `local-review` | off | 2048 | 0 / 2048 | current reviewer |
| `local-reason` | on | 2048 | 1024 / 1024 | optional bounded planner candidate |

The reasoning route starts with temperature `0.6`, top-p `0.95`, top-k `20`, repetition
penalty `1.05`, a 300-second timeout, no provider retry, no reasoning-history
preservation, and an explicit model-switch requirement. These are qualification inputs,
not proof that the route is suitable or a new default.

Additional bounded examples cover exact probing, reviewer work, and long-form diagnostics.
All provider-reported completion tokens remain part of the existing completion-token
budget, including tokens consumed before final content. Run `make route-profile-check` to
verify profile validation and adapter wiring without starting any model service.
