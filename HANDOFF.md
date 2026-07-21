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

The current audit corpus is small and heterogeneous. It exposes process failures but is
not yet a fitness dataset. Model-metric and step tables exist but are not populated or
fully returned by run inspection.

## Main gap

The repository can safely execute and record one bounded run. It cannot yet:

- normalize outcomes across runs;
- define a trustworthy fitness function;
- compare baseline and candidate generations;
- enforce campaign-wide time, token, process, or disk budgets;
- separate development cases from candidate-inaccessible holdout oracles;
- record candidate lineage, environment identity, or promotion decisions;
- turn repeated failures into bounded improvement briefs.

Adding more agent autonomy before this control plane exists would optimize against noisy,
candidate-controlled evidence.

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

## Working roadmap

### 1. Trusted measurement layer

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

### 2. Frozen paired evaluator

Create a small development suite and a separate candidate-inaccessible holdout suite.
Cover exact edits, multi-edit atomicity, missing and ambiguous matches, scope leakage,
malformed editor and reviewer output, verification failure, and complete diff capture.

Run baseline and candidate sequentially against the same service configuration. Record
manifest and oracle hashes, repetitions, environment identity, budgets, and per-case
results. Trusted base-owned contracts must run against the candidate in addition to the
candidate's own verification command.

### 3. Failure miner and improvement brief

Cluster normalized failures and emit exactly one strict improvement brief with:

- evidence run IDs and baseline commit;
- one failure class and falsifiable hypothesis;
- allowed and forbidden files;
- predeclared acceptance metrics and suite hash;
- hard budget and rollback condition.

Initially a human approves every brief. New cases derived from real failures enter the
visible development suite first; only a human can promote them to holdout.

### 4. Candidate experiments

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

### 5. Promotion scorecard

Promotion is lexicographic, never a scalar tradeoff:

1. **Safety:** zero protected-path, scope, staged/untracked, or control violations.
2. **Correctness:** trusted oracle and deterministic verification pass.
3. **Regression:** no holdout case becomes worse.
4. **Control:** zero rejected edits, bounded retries, and fresh independent review.
5. **Improvement:** the predeclared target improves across repeated paired trials.
6. **Efficiency:** time, tokens, model calls, and tool calls remain within budget.
7. **Authority:** a human explicitly commits and promotes the candidate.

### 6. Bounded recursion

Begin with one proposal and one candidate per campaign. Increase to a maximum of three
iterations only after at least ten clean campaigns with zero safety regressions. Archive
lineage, patches, hypotheses, scorecards, and human decisions. Do not retain unlimited
active worktrees or allow an unbounded daemon loop.

## First implementation slice

Build only the read-only measurement and comparison substrate:

- `evaluation/outcomes.py` — normalized outcomes and failure taxonomy;
- `evaluation/supervisor.py` — trusted baseline/candidate command runner with hard limits;
- `evaluation/suites/atomic-v1.json` — visible development cases;
- additive state methods and schema versioning in `runtime/state.py`;
- read-only `analyze-runs` and `evaluate` CLI commands;
- protected `tests/test_evaluation_contract.py` and deterministic unit tests.

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
./local-coder.py status
./local-coder.py run "Implement one atomic task with explicit files and acceptance criteria"
```

Inspect every returned worktree, run record, diff, verification result, and fresh review
state. Do not merge automatically. Broad autonomous decomposition remains outside the
validated capability of the current 3B model.
