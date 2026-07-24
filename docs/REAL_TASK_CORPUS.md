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

Do not commit, print, or expose the trusted holdout payload outside the trusted scorer.
Do not use holdout results while tuning routes or prompts. G2 establishes the current-route
baseline, G3/G3.1 freeze the selected Qwythos generation and prompt contracts, and G4 is the
only model-running surface permitted to load the payload for the final independent decision.

The supplied holdout archive should be extracted at the repository root. Its payload path
is already ignored by Git.

## Development evidence collection

`evaluation/real_task_development.py` runs every development case through the production
`PlannerProgram` or `ReviewerProgram` with `dspy.JSONAdapter`. It does not accept a
holdout path. The frozen protocol in `profiles/track-g-development-v1.json` binds the
development suite hash, scoring version, Qwen baseline routes, Qwythos candidate route,
and exact runtime profiles.

Validate the runner without model services:

```bash
make real-task-development-check
```

After committing the tooling and starting the Qwen `local-coder` server plus LiteLLM,
collect the G2 baseline:

```bash
make real-task-development-collect \
  SUBJECT=baseline \
  ENVIRONMENT=amelia-gtx1660-v1
```

Reports are written beneath `.local-coder/real-task-evidence/`. They retain case IDs,
case hashes, bounded scoring dimensions, failure codes, latency, and available token
counts. Generated planner/reviewer fields, final answers, prompts, and reasoning text are
not persisted. Collection requires a clean committed tree and fails if active prompt
lineage changes during the run.

## G3 accuracy-first profile tuning

The Qwen and Qwythos G2 development reports showed a small aggregate Qwythos advantage
with mixed per-case movement. G3 then compared three Qwythos generation profiles on
development only. Greater reasoning depth did not improve mean quality and no selected role
cleared the frozen development gain gate. See
[Qwythos profile tuning](QWYTHOS_PROFILE_TUNING.md).

## G3.1 prompt-contract tuning

The repeated G3 failure classes pointed to instruction completeness rather than reasoning
budget. G3.1 held generation settings constant and selected `evidence-completeness` for
planner and `field-checklist` for reviewer. Both cleared the frozen role-wise holdout gate.
See [Qwythos prompt tuning](QWYTHOS_PROMPT_TUNING.md).

## G4 one-shot holdout qualification

`evaluation/real_task_holdout.py` is the only runner that accepts the trusted payload. It
binds the G3.1 selection hash, validates all non-holdout controls before loading the suite,
creates exclusive per-subject consumption receipts, and always runs all four cases exactly
once. The final comparison qualifies planner and reviewer independently and never changes
an active route. See
[Qwythos holdout qualification](QWYTHOS_HOLDOUT_QUALIFICATION.md).

Validate both tuning controls without model services:

```bash
make real-task-profile-tuning-check
make real-task-prompt-tuning-check
make real-task-holdout-check
```

Both collectors use the production programs, JSON adapter, scorer, clean implementation
commit, and unchanged service identity. Neither command accepts a holdout path.
