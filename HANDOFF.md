# Recursive Improvement Handoff

## Direction

The next phase is an evidence-gated recursive improvement loop for the local coding
harness. The target is a system that can diagnose its own failures, propose one bounded
improvement, build and test a candidate, and recommend promotion. It must never authorize
its own promotion.

Recursive improvement applies to the scaffold—skills, context selection, tool protocols,
control flow, and eventually logical route selection—not to autonomous foundation-model
training on this hardware.

## Current architecture

```text
Developer
   ↓
local-coder role-separated harness
   ├── explorer and planner       → read-only evidence → local-plan
   ├── implementer and repairer   → native exact edits → local-fast
   └── reviewer                   → fixed read-only adapter → local-review
        ↓
Git worktree isolation + SQLite audit
        ↓
LiteLLM stable logical routes
        ↓
llama.cpp + Qwen2.5-Coder-3B Q4_K_M
```

The architecture remains local-first and hardware-adjusted. `runtime/editor.py` is the
only source-editing worker during local agent runs. Deterministic verification and human
approval remain authoritative.

Supporting documentation:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/PIPELINE.md`](docs/PIPELINE.md)
- [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md)
- [`docs/VALIDATION_HISTORY.md`](docs/VALIDATION_HISTORY.md)

## Current evidence

The existing runtime has proven:

- complete fail-before-write validation of exact edit batches;
- protected and approved path enforcement;
- tracked, staged, untracked, and symbolic-link diff visibility;
- isolated Git worktrees with preserved uncommitted changes;
- deterministic verification independent of model judgement;
- fixed read-only review with fresh verdict state;
- conservative `needs_attention` handling for rejected edits, scope violations, and
  unavailable review;
- SQLite records for runs, roles, tool calls, artifacts, and verification.

The current audit corpus remains small and heterogeneous. New runs populate step and
model-metric records where APIs expose usage, while historical missing values remain
unknown. Run inspection now returns the complete evidence needed for normalization.

## Recursive-improvement control plane

The trusted control plane is implemented under `evaluation/`:

- historical runs normalize into versioned outcomes without treating missing metrics as
  zero;
- ordered transactional SQLite migrations preserve old run data and record run identity,
  campaigns, briefs, paired cases, scorecards, approvals, and decisions;
- development cases and externally rotated holdout oracles are loaded and hashed
  independently;
- candidate verification and base-owned contracts run sequentially in networkless,
  read-only bubblewrap sandboxes with time, kernel process-count, memory, output, file,
  disk, token, and model-call budgets;
- the failure miner emits one deterministic brief from allowlisted structured facts;
- campaigns require explicit human brief approval and allow one candidate until ten
  clean campaigns justify a maximum of three;
- promotion scorecards apply ordered safety, correctness, regression, control,
  improvement, efficiency, and human-authority gates.

The system recommends promotion but does not commit, merge, push, promote, create an
evaluation worktree, or clean one up. See
[`docs/RECURSIVE_IMPROVEMENT.md`](docs/RECURSIVE_IMPROVEMENT.md) for the operator flow.

## Target loop

```text
Promoted generation G(n)
        ↓
real tasks + frozen benchmark
        ↓
trusted trajectory evaluator
        ↓
failure clusters → one bounded improvement brief
        ↓
candidate in an isolated worktree
        ↓
paired baseline/candidate evaluation
        ↓
hard gates + scorecard
        ↓
human promotes or rejects
        ↓
Promoted generation G(n+1)
```

The evaluator must execute from a trusted baseline checkout. Candidate code must not
control evaluation contracts, holdout cases, oracle answers, promotion policy, or their
hashes.

## Non-negotiable boundaries

- Keep llama.cpp, LiteLLM, and logical routes `local-fast`, `local-plan`, and
  `local-review`.
- Keep the native atomic editor as the only agent source-editing boundary.
- Keep Git worktrees as the isolation boundary and SQLite as the audit store.
- Never add automatic commit, merge, push, promotion, or destructive cleanup.
- Never let a candidate edit trusted evaluator code, contracts, holdout manifests,
  oracles, or promotion policy.
- Never accept candidate-owned `make verify` as the sole oracle.
- Treat stored tasks, model responses, and tool output as untrusted data; pass only
  allowlisted structured facts into improvement prompts.
- Run one bounded candidate at a time on the current hardware.
- Do not download a larger model or change a hardware profile without explicit user
  authorization and benchmark evidence.

## Implemented roadmap

### 1. Trusted measurement layer — implemented

Add a read-only `evaluation/` package and CLI reporting commands. Normalize every run into
a structured outcome containing:

- baseline commit and task, suite, diff, model, route, skill, and configuration hashes;
- expected and actual changed paths;
- verification, oracle, review, and policy results;
- tool, editor, reviewer, scope, and budget failures;
- wall time, tokens, model calls, verification count, and unknown metrics.

Use additive, versioned SQLite migrations. Wire the existing `steps` and `model_metrics`
tables before creating overlapping storage. The first slice must work without model
services and must not mutate a repository or create a worktree.

### 2. Frozen paired evaluator — implemented

Create a small development suite and a separate candidate-inaccessible holdout suite.
Cover exact edits, multi-edit atomicity, missing and ambiguous matches, scope leakage,
malformed editor and reviewer output, verification failure, and complete diff capture.

Run baseline and candidate sequentially against the same service configuration. Record
manifest and oracle hashes, repetitions, environment identity, budgets, and per-case
results. Trusted base-owned contracts must run against the candidate in addition to the
candidate's own verification command.

### 3. Failure miner and improvement brief — implemented

Cluster normalized failures and emit exactly one strict improvement brief with:

- evidence run IDs and baseline commit;
- one failure class and falsifiable hypothesis;
- allowed and forbidden files;
- predeclared acceptance metrics and suite hash;
- hard budget and rollback condition.

Initially a human approves every brief. New cases derived from real failures enter the
visible development suite first; only a human can promote them to holdout.

### 4. Candidate experiments — implemented boundary

Start with low-risk improvement surfaces:

1. structured planner-to-implementer handoff;
2. editor and reviewer structured-output reliability;
3. duplicate verification and review for identical diff hashes;
4. evidence selection and context compression;
5. skill prompt and step-budget variants.

Prefer in-memory overlays for prompt and skill experiments. Source candidates use the
existing native editor and remain uncommitted until human review. A human-created
experiment commit is required before full generational comparison under the current
no-automatic-commit policy.

### 5. Promotion scorecard — implemented

Promotion is lexicographic, never a scalar tradeoff:

1. **Safety:** zero protected-path, scope, staged/untracked, or control violations.
2. **Correctness:** trusted oracle and deterministic verification pass.
3. **Regression:** no holdout case becomes worse.
4. **Control:** zero rejected edits, bounded retries, and fresh independent review.
5. **Improvement:** the predeclared target improves across repeated paired trials.
6. **Efficiency:** time, tokens, model calls, and tool calls remain within budget.
7. **Authority:** a human explicitly commits and promotes the candidate.

### 6. Bounded recursion — implemented

Begin with one proposal and one candidate per campaign. Increase to a maximum of three
iterations only after at least ten clean campaigns with zero safety regressions. Archive
lineage, patches, hypotheses, scorecards, and human decisions. Do not retain unlimited
active worktrees or allow an unbounded daemon loop.

## Delivered artifacts

- `evaluation/outcomes.py` — normalized outcomes and failure taxonomy;
- `evaluation/supervisor.py` — trusted baseline/candidate command runner with hard limits;
- `evaluation/suites/atomic-v1.json` — visible development cases;
- `.local-coder/holdout/` — ignored, immutable per-rotation holdout inputs provisioned
  from an external source by `rotate-holdout`;
- `evaluation/miner.py` and `evaluation/scorecard.py` — one-brief mining and ordered
  promotion gates;
- `evaluation/audit.py` — read-only final campaign invariant audit;
- additive state methods in `runtime/state.py` and ordered migrations in
  `runtime/migrations.py`;
- read-only analysis and repository-read-only `evaluate` CLI commands;
- protected `tests/test_evaluation_contract.py` and deterministic unit tests.

## Completion status

The recursive-improvement control plane is complete at the bounded, human-gated scope
defined in this handoff.
Evaluation lineage now binds one campaign evaluation to one unique candidate-build ID.
Its control gate fails closed on rejected edits, tool errors, excessive retries,
`needs_attention`, stale review, failed final verification, or missing/over-budget model
usage. Schema v8 is applied through an ordered, atomic ledger with structural and
foreign-key compatibility checks; malformed, gapped, future, and partially migrated
databases fail without partial writes.

Candidate builds now share one enforced call/prompt/completion-token budget across every
role. Missing usage fails closed, and an over-budget response is recorded before the run
stops. Sandbox commands execute as an unprivileged UID with capabilities dropped and a
hard `RLIMIT_NPROC` installed by the base-owned `process_guard.py`. Production holdout
inputs are no longer tracked: each immutable rotation is copied from an external source
into ignored trusted storage and campaign commands reject candidate-visible holdout
paths.

Campaign creation freezes the holdout manifest-plus-oracle identity and evaluator
environment identity in addition to the suite and budget. Mismatches fail before
candidate execution. Candidate patch and trajectory artifacts are persisted before
sandbox execution, including terminal budget or process failures.

The deterministic campaign control-cycle demonstration covers brief approval,
candidate-build lineage, paired evidence, scorecard recommendation, human decision,
campaign closure, and a read-only final invariant audit. `audit-campaign` validates
identity binding, artifact hashes, paired cases, scorecard order, bounded candidates,
human authority, and terminal safety/regression status without modifying Git or SQLite.

Acceptance criteria:

- existing SQLite data migrates without loss;
- existing historical runs normalize deterministically;
- missing model metrics remain `unknown`, not zero;
- candidate test tampering cannot replace base-owned contracts;
- manifest and environment mismatches fail closed;
- timeouts and nonzero process exits are recorded, not retried indefinitely;
- no command edits, commits, merges, pushes, promotes, or deletes anything;
- `make verify`, `make agent-smoke`, and clean-tree `make handoff-check` pass.

## Current operating procedure

```bash
make verify
make agent-smoke
./local-coder.py analyze-runs
./local-coder.py status
./local-coder.py audit-campaign CAMPAIGN_ID
./local-coder.py run --expected-file FILE \
  "Implement one atomic task with explicit files and acceptance criteria"
```

Inspect every returned worktree, run record, diff, verification result, and fresh review
state. Do not merge automatically. Broad autonomous decomposition remains outside the
validated capability of the current 3B model.
