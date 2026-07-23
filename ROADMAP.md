# ROADMAP

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active work only

This file is the single queue for unfinished engineering work. Completed architecture,
implementation history, and operating procedures belong in `docs/` and should not be
expanded here.

## Completed foundation

The following programmes are complete and retained as stable repository capabilities:

- portable Agent Skill discovery, activation, packaging, and linting;
- LiteLLM-backed typed DSPy programs for all five specialist roles;
- audited GEPA dataset export and bounded offline prompt optimization;
- prompt campaigns with explicit candidate outcomes and hard model-call accounting;
- paired development and external holdout evaluation;
- scorecard-derived authorization, campaign close, and read-only audit;
- promotion-bound prompt activation, hash-verified runtime loading, and rollback; and
- one-command prompt lifecycle completion that never activates a rejected candidate.

Completion records and operator references:

- [`docs/HANDOFF.md`](docs/HANDOFF.md)
- [`docs/RECURSIVE_IMPROVEMENT.md`](docs/RECURSIVE_IMPROVEMENT.md)
- [`docs/GEPA_CAMPAIGNS.md`](docs/GEPA_CAMPAIGNS.md)
- [`docs/PROMPT_DEPLOYMENT.md`](docs/PROMPT_DEPLOYMENT.md)

## Active programme roadmap

**Current primary programme: Track E — MCP control-plane integration**

The detailed implementation plan lives in
[`docs/roadmaps/MCP_CONTROL_PLANE.md`](docs/roadmaps/MCP_CONTROL_PLANE.md). It adds an
optional MCP server over the existing trusted-planner CLI without adding MCP tools to the
internal agent runtime, changing source-write authority, or making the local loop depend
on an external service. Tracks A–D are complete and retired; Track E is globally reserved
for this programme.

## R1 — Deployment safety and recovery

**Priority:** queued after Track E unless deployment recovery becomes urgent

Strengthen the active prompt store without changing candidate-construction or evaluation
semantics.

- Verify the activated role immediately through a bounded runtime health check.
- Roll back automatically when post-activation verification fails.
- Recover deterministically after interruption between history persistence and active
  pointer replacement.
- Detect missing, malformed, escaped, or hash-drifted active prompt state during startup
  and inspection.
- Archive activation-health and rollback evidence under the existing audit lineage.
- Add one idempotent recovery command suitable for unattended operator workflows.

**Exit criteria:** activation is atomic, interruption-safe, health-checked, auditable, and
reversibly restores the last verified state without candidate-controlled behavior.

## R2 — Regression-aware prompt candidate selection

**Priority:** deferred until another optimization cycle is useful

Prevent aggregate development gains from hiding per-case regressions before external
holdout evaluation.

- Record baseline and candidate scores for each development case.
- Require a strict aggregate improvement with zero regressed development cases.
- Persist minimum case delta and regression count in the candidate artifact.
- Reject mean-improving candidates that degrade any frozen development case.
- Keep the external holdout independent and unavailable to optimization or selection.

**Exit criteria:** a `candidate_ready` state improves the frozen development mean and does
not reduce any development case.

## R3 — Prompt evaluation stability

**Priority:** deferred; inference-expensive

Reduce one-sample model noise without making the local workflow unbounded.

- Add operator-selected repeated paired replays.
- Record score variance and result stability.
- Define bounded confidence or consensus requirements for promotion eligibility.
- Share strict call and token budgets across repetitions.
- Preserve the current fail-closed external holdout gate.

**Exit criteria:** unstable candidates cannot pass through a single favorable sample, and
all added inference remains explicitly bounded.

## R4 — Optional reflection capacity

**Priority:** optional

Allow an operator-selected reflection route or stronger external model for offline GEPA
while preserving the trusted-evaluator boundary.

- Keep the default local route unchanged.
- Require explicit configuration outside candidate-controlled state.
- Record provider, route, model identity, token usage, and retry-accounting limits.
- Never make a cloud dependency mandatory for the local coding loop.

## R5 — Skills ecosystem work

**Priority:** optional

- Cross-test the portable role skills in other compatible runtimes.
- Publish hardware-neutral variants only when their behavior remains bounded.
- Treat imported third-party skills as read-only prompt data, never executable tools.

## Permanent constraints

- Keep llama.cpp, LiteLLM, and logical routes `local-fast`, `local-plan`, and
  `local-review`.
- Keep the native exact editor as the only agent source-writing boundary.
- Keep Git worktrees for run isolation and SQLite for audit lineage.
- Do not add automatic source commit, merge, push, or destructive worktree cleanup.
- Do not expose candidate-visible holdout material or candidate-controlled evaluation.
- Do not make the core local loop depend on a cloud service.
- Keep all resource-intensive work bounded for the current hardware.

## Definition of done for roadmap items

Every completed item must leave these gates green where applicable:

```bash
make verify
make agent-smoke
make skills-lint
make gepa-runner-check
make prompt-campaign-check
make prompt-deployment-check
git diff --check
```

Move completed implementation detail into the appropriate stable document under `docs/`.
Do not leave finished task narratives in this file.
