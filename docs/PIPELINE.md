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

## Model Profiles

* Fast profile: Qwen2.5-Coder-3B-Instruct Q4_K_M, API alias `local-coder`
* Reasoning profile: Qwythos 9B Q4_K_M, API alias `local-reason`
* Server endpoint: `http://127.0.0.1:8080/v1`
* Server context: 32,768 tokens
* Parallel slots: 1
* Residency: one trusted physical profile at a time

The committed role activation and model-service policy select and verify the physical
profile before every model request. Machine-specific binary and model directories may be
overridden, but the frozen filenames, route mapping, and launch policy remain bound.

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

The frozen Track G comparison qualified Qwythos for planner and reviewer use. Those two
roles now use qualification-bound prompts and generation profiles through `local-reason`;
orchestration, exploration, implementation, and repair remain on Qwen. Prompt optimization
is retained, but generic active prompt state cannot override the two G4-qualified role
profiles.

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

## Repository context compilation

Before Explorer or Planner invokes its typed DSPy programme, the runtime derives a bounded
query plan from the authoritative task and delegated request. Filename, path, exact text,
regex, symbol, behavior-term, and changed-file signals route through trusted adapters.
Results from current-worktree ripgrep, committed Zoekt indexes, Universal Ctags, and Git
state are merged with current dirty paths taking priority.

Selected source ranges are reread from current bytes, line-numbered, clipped to the role
policy, and hashed. Explorer receives broader short-range evidence; Planner receives fewer,
longer definition and test ranges. The DSPy programme remains one-shot. If persistent search
backends are absent or stale, retrieval falls back to ripgrep and then a bounded Git scanner
without blocking the edit pipeline.

Search lineage is recorded as a run artifact, including queries, selected paths and ranges,
content hashes, unresolved terms, truncation, timings, and backend failures. Repository
content is not duplicated into the SQLite lineage record.

## Role-Separated Agent Runtime

The primary runtime is now a smolagents manager with managed explorer, planner,
implementer, repairer, and reviewer agents. It composes the native editor, LiteLLM,
worktree, verification, and review components.

```bash
make agent-install
./local-coder.py run "Implement one concrete task"
./local-coder.py run "Inspect a shared contract" --search-repo registered-id
```

The command requires a clean base repository, creates an isolated sibling worktree, and
leaves all edits uncommitted for independent inspection. Registered repositories may
be attached repeatedly with `--search-repo`; they supply read-only Explorer and Planner
context and never expand editable paths. Each editor call validates a
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

Base generation policy is defined in `runtime/route_profiles.py`. Qualified role
overrides are derived from the frozen G4 protocol by `runtime/role_profiles.py`; they are
not inferred from role names or editable task content.

| Role | Route | Reasoning / final | Temperature | Physical model |
|---|---|---:|---:|---|
| Orchestrator / explorer | `local-plan` | 0 / 3072 | 0.0 | Qwen |
| Planner | `local-reason` | 1024 / 1024 | 0.6 | Qwythos |
| Implementer / repairer | `local-fast` | 0 / 2048 | 0.0 | Qwen |
| Reviewer | `local-reason` | 1536 / 1536 | 0.0 | Qwythos |

`runtime/model_service.py` serializes every physical switch. It validates the exact trusted
launch profile, refuses unknown live processes, records switch evidence, and restores the
previous recognized profile after a failed load. Normal role calls trigger this
synchronously; no background supervisor or simultaneous 3B/9B residency is assumed.

All provider-reported completion tokens remain part of the existing completion-token
budget, including tokens consumed before final content. Run `make route-profile-check`,
`make role-profile-check`, and `make model-service-check` without starting a model service.
See [`MODEL_SWITCHING.md`](MODEL_SWITCHING.md) for live operation.
