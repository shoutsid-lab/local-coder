# Qwythos development prompt-contract tuning

## Why this experiment exists

The G3 generation-profile comparison completed without opening holdout. More reasoning
budget did not improve planner quality. The recurring failures were stable across profiles:
planner acceptance criteria omitted concrete evidence, out-of-scope paths leaked into plan
text, and one project-specific reviewer case did not classify an unrelated README change.
That pattern identifies the reusable role instruction as the next bounded variable.

This experiment changes prompt instructions only. It does not change the model, corpus,
scorer, DSPy output schema, active route, or sealed holdout.

## Frozen generation settings

[`../profiles/track-g-qwythos-prompt-tuning-v1.json`](../profiles/track-g-qwythos-prompt-tuning-v1.json)
uses the strongest development settings from G3:

- planner: the `current-control` generation profile, which retained the highest planner mean;
- reviewer: the deterministic profile, which tied the best reviewer mean with higher stable
  case success and lower cost.

Every prompt profile runs all eight visible cases twice.

## Frozen prompt profiles

- `code-control` uses the code-defined `PlannerSignature` and `ReviewerSignature`
  instructions unchanged.
- `evidence-completeness` requires concrete evidence-backed acceptance criteria, forbids
  out-of-scope path mentions, and makes unrelated changed paths explicit reviewer failures.
- `field-checklist` expresses the same role contracts as a concise output-field checklist.

The candidate instructions are reusable role-level constraints. They contain no case IDs,
example outputs, holdout content, or generated model text.

## Isolation and evidence

The collector requires no active deployed planner or reviewer prompt state. It instantiates
the production `PlannerProgram` and `ReviewerProgram`, applies an inert instruction override
to their single predictor, and runs with `dspy.JSONAdapter` and the frozen generation
profiles.

Reports retain only case IDs, bounded scoring dimensions, failure codes, latency, and
available token counts. They retain no generated fields, prompts, final answers, or
reasoning text. The command exposes no holdout path.

## Collect

```bash
make real-task-prompt-tuning-collect \
  PROMPT_PROFILE=code-control \
  ENVIRONMENT=amelia-gtx1660-v1

make real-task-prompt-tuning-collect \
  PROMPT_PROFILE=evidence-completeness \
  ENVIRONMENT=amelia-gtx1660-v1

make real-task-prompt-tuning-collect \
  PROMPT_PROFILE=field-checklist \
  ENVIRONMENT=amelia-gtx1660-v1
```

Use one clean implementation commit, the same Qwythos server, and unchanged LiteLLM
configuration for all three runs.

## Compare

```bash
CONTROL="$(ls -1t \
  .local-coder/real-task-evidence/code-control-track-g-prompt-tuning-v1-*.json | \
  head -n 1)"
EVIDENCE="$(ls -1t \
  .local-coder/real-task-evidence/evidence-completeness-track-g-prompt-tuning-v1-*.json | \
  head -n 1)"
CHECKLIST="$(ls -1t \
  .local-coder/real-task-evidence/field-checklist-track-g-prompt-tuning-v1-*.json | \
  head -n 1)"

make real-task-prompt-tuning-compare \
  REPORTS="$CONTROL $EVIDENCE $CHECKLIST" \
  OUTPUT=.local-coder/real-task-evidence/qwythos-prompt-selection-v1.json
```

The same accuracy-first ranking and no-material-regression holdout gate apply. A selected
prompt remains inert. Promotion and activation are not performed by this experiment.
