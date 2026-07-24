# ROADMAP

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active work only

This file is the single queue for unfinished engineering work. Completed architecture,
implementation history, and operating procedures belong in `docs/` and should not be
expanded here.

## Strategic reset: capability before more control-plane work

The repository has strong editing, isolation, verification, evaluation, and authorization
boundaries. Those controls are retained. The current evidence does **not** yet show that
prompt optimization materially improves real coding work: the first live planner candidate
was rejected after regressing three external holdout cases.

Until stronger evidence exists, engineering priority moves to the capability bottleneck:

1. qualify a reasoning-capable planner and reviewer route;
2. build a benchmark from real repository tasks and failures;
3. measure task success, schema adherence, latency, and repair cost; and
4. only then decide whether MCP plumbing, GEPA expansion, or deployment hardening earns
   further investment.

This is a moratorium on **new control-plane features**, not a rollback of existing safety
boundaries. Existing campaign, audit, activation, and rollback code remains supported.

## Completed foundation

The following capabilities are complete and retained:

- portable Agent Skill discovery, activation, packaging, and linting;
- typed DSPy programs for all five specialist roles;
- validated exact editing, isolated worktrees, deterministic verification, and review;
- bounded source and prompt campaigns with independent authorization;
- paired development and external holdout evaluation;
- prompt activation, hash-verified loading, and rollback; and
- fail-closed audit and unattended lifecycle completion.

The concise historical index is [`docs/HISTORY.md`](docs/HISTORY.md). Detailed retained
records remain available but are not required reading for routine work.

## Active priority 1: Track F — reasoning-capable model routes

**Current primary programme.**

The detailed plan lives in
[`docs/roadmaps/REASONING_MODEL_ROUTES.md`](docs/roadmaps/REASONING_MODEL_ROUTES.md).
It first fixes reasoning/final-content contracts, then qualifies an optional
`local-reason` route for planner and reviewer use. The existing 3B implementer and repairer
remain the default fast coding path.

F0–F2 and the first F3 policy/resource slice are complete. The first live Qwythos run
verified final-answer completion, reasoning presence, 8K context handling, throughput,
and current-machine memory use. It also exposed that the v1 focused collector conflated
schema validity with fixture-specific task correctness.

The raw-route v2 comparison is retained as native-output diagnostic evidence. The
shared-adapter comparison then ran Qwen and Qwythos through the same `PlannerProgram`,
`ReviewerProgram`, and `JSONAdapter`: both routes passed the smoke contract, while Qwythos
used materially more latency and completion tokens. That establishes compatibility, not
superior capability.

The frozen Track G holdout completed on 2026-07-24. Qwythos qualified under the
preregistered policy for both planner and reviewer with no case-level regression. F4 and F5
now contain the bounded activation implementation: synchronous serial model switching,
qualification-bound planner/reviewer prompts, and Qwen retained for orchestration,
exploration, implementation, and repair. Live switch-cycle and bounded-run validation on
the target machine remain before Track F is closed. F6 MTP benchmarking remains optional.

## Completed capability evidence: Track G — real-task benchmark and holdout

Track G runs alongside Track F. The detailed plan lives in
[`docs/roadmaps/REAL_TASK_EVIDENCE.md`](docs/roadmaps/REAL_TASK_EVIDENCE.md).

G0 and G1 froze a versioned 12-case corpus from actual repository tasks, failures, and
successful fixes: eight development cases plus a four-case sealed holdout. G2 collected
comparable Qwen and Qwythos evidence through production adapters. G3 found no gain from
larger reasoning budgets. G3.1 isolated the prompt-contract bottleneck and selected
`evidence-completeness` for planner and `field-checklist` for reviewer. G4 then consumed the
one-shot holdout exactly once. Qwythos improved planner mean score from 0.5833 to 0.6667 and
reviewer mean score from 0.6 to 0.8, with no case regression, so both frozen role gates
passed.

Track G is complete. Its normalized baseline, candidate, and final reports are committed
under `evidence/track-g/` and must not be regenerated or used for further tuning. Track F
owns the separate bounded activation and live model-switch validation. Synthetic sentinel
edits remain smoke fixtures rather than primary capability evidence.

## Queued programme: Track E — MCP control-plane integration

The detailed plan remains in
[`docs/roadmaps/MCP_CONTROL_PLANE.md`](docs/roadmaps/MCP_CONTROL_PLANE.md), but Track E is
queued behind the Track F/G capability milestone.

MCP is useful operator transport, not a capability multiplier. Read-only groundwork may
proceed opportunistically when it does not delay route qualification or real-task evidence.
Gated MCP write tools remain deferred until the benchmark path is established.

## Evidence gate for optimization and deployment expansion

Do not add new GEPA selection logic, repeated-evaluation machinery, automatic activation
recovery, or further campaign kinds until at least one of these conditions is met:

- a non-synthetic prompt campaign produces a candidate that passes independent holdout and
  is explicitly promoted; or
- Track G evidence shows that prompt or deployment behavior, rather than model capability,
  is the measured bottleneck.

The current GEPA and deployment paths remain available for bounded experiments and bug
fixes. Correctness, security, and compatibility defects are not blocked by this gate.

## R1 — Deployment safety and recovery

**Status:** frozen behind the evidence gate

Potential work includes post-activation health checks, interrupted-write recovery, drift
detection, and automatic rollback. Resume only when an active promoted prompt exists or a
real deployment defect makes the work necessary.

## R2 — Regression-aware prompt candidate selection

**Status:** frozen behind the evidence gate

Per-case development deltas and zero-regression selection remain sensible, but do not add
this machinery until a non-synthetic campaign demonstrates that prompt search is worth
continuing.

## R3 — Prompt evaluation stability

**Status:** frozen behind the evidence gate

Repeated paired replay, variance estimates, and confidence thresholds are inference-heavy.
Resume only after a candidate reaches promotion contention on real tasks.

## S1 — State schema stabilization

**Priority:** after the Track F/G capability milestone

Plan one explicit pre-stable schema reset rather than carrying development migrations
forever.

- Export any campaign or validation evidence worth retaining.
- Define a clean current SQLite schema and supported reset procedure.
- Decide whether pre-stable local databases are recreated rather than upgraded.
- Remove obsolete compatibility branches and migrations only after the new boundary is
  documented and tested.
- Keep stable releases migration-compatible after the reset point.

**Exit criteria:** the project has one documented schema baseline, a deterministic local
reset/export path, and no accidental promise to support every development database shape
indefinitely.

## R5 — Skills ecosystem work

**Priority:** optional

- Cross-test portable role skills in other compatible runtimes.
- Publish hardware-neutral variants only when behavior remains bounded.
- Treat imported third-party skills as read-only prompt data, never executable tools.

## Permanent constraints

- Keep llama.cpp, LiteLLM, and the existing logical routes `local-fast`, `local-plan`, and
  `local-review`; `local-reason` must be additive, optional, and qualified before changing
  defaults.
- Keep the native exact editor as the only agent source-writing boundary.
- Keep Git worktrees for run isolation and SQLite for audit lineage.
- Do not add automatic source commit, merge, push, or destructive worktree cleanup.
- Do not expose candidate-visible holdout material or candidate-controlled evaluation.
- Do not make the core local loop depend on a cloud service.
- Keep resource-intensive work bounded for the current hardware.
- Treat candidate-neutral authorization as integrity protection and future-capability
  insurance, not evidence that the current 3B model is an active adversary.

## Definition of done for roadmap items

Every completed item must leave these gates green where applicable:

```bash
make verify
make agent-smoke
make skills-lint
make route-probe-check
make gepa-runner-check
make prompt-campaign-check
make prompt-deployment-check
git diff --check
```

Move completed implementation detail into the appropriate stable or historical document
under `docs/`. Do not leave finished task narratives in this file.
