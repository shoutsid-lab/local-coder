# Real-task corpus operator reference

## Scope

Track G v1 freezes 12 planner/reviewer cases derived from actual local-coder work:

- eight candidate-visible development cases;
- four candidate-inaccessible holdout cases;
- six planner and six reviewer cases overall; and
- the exact class minimums declared in the Track G roadmap.

The corpus is designed to replace repeated toy probes as capability evidence. It includes
multi-file reasoning-contract work, state/evidence selection, route-construction drift,
lint handoff failures, reviewer false-positive traps, stale review state, write-boundary
regressions, and documentation-interface consistency.

## Trusted files

Committed candidate-visible controls:

```text
evaluation/real_task_cases/development-v1.json
evaluation/real_task_cases/holdout-v1.index.json
```

Separately provisioned trusted holdout payload:

```text
.local-coder/real-task-holdout/holdout-v1.json
```

The holdout index exposes only case ID, role, class, bounded tags, difficulty, baseline
kind, provenance reference, pattern group, and canonical hashes. It contains no task,
input, successful outcome, or oracle fields.

## Validation

Validate the committed split without opening holdout content:

```bash
make real-task-corpus-check
```

Validate the separately installed holdout payload against the committed hashes:

```bash
make real-task-corpus-check \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json
```

Print the non-sensitive corpus summary:

```bash
make real-task-corpus-summary
```

The validator rejects malformed schemas, duplicate case IDs, duplicate pattern groups,
development/holdout leakage, class under-coverage, unsafe repository paths,
machine-specific paths, unbound evidence snapshots, and altered holdout payloads.

## Case contract

Every complete case binds:

- one role: planner or reviewer;
- one primary Track G class and bounded descriptive tags;
- an immutable Git commit, archived run, or exact evidence-snapshot identity;
- the exact production-program inputs;
- expected editable scope and deterministic verification commands;
- provenance and the known successful outcome; and
- a role-specific oracle used only by trusted scoring.

Planner cases use the `PlannerProgram` input fields and require exact editable files plus
bounded instruction and acceptance terms. Reviewer cases use the `ReviewerProgram` input
fields and freeze the expected verdict and required issue/unrelated paths.

## Holdout handling

Do not commit, print, mount, or prompt with the trusted holdout payload. Do not use holdout
results while tuning routes or prompts. G2 establishes the current-route baseline on the
development set. G3 performs frozen comparisons and opens the holdout only for the final
independent decision.

The supplied holdout archive should be extracted at the repository root. Its payload path
is already ignored by Git.
