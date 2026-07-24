# Qwythos one-shot holdout qualification

**Status:** Completed on 2026-07-24 at implementation commit
`0a0825cc8ae2622d2a82f5e87088077827cd62b9`. Both frozen role gates passed;
route activation is recorded separately.

## Purpose

G4 performs the final independent Track G comparison between the incumbent Qwen
planner/reviewer routes and the selected Qwythos role configuration. It is the first and
only model-running surface that accepts the trusted `holdout-v1` payload.

The runner does not activate prompts or change routes. It produces a role-wise
qualification claim that later Track F work may use alongside model-switch and lifecycle
evidence.

## Frozen inputs

[`../profiles/track-g-holdout-qualification-v1.json`](../profiles/track-g-holdout-qualification-v1.json)
binds:

- the committed holdout index and sealed suite hashes;
- the exact G3.1 prompt-selection comparison hash;
- one attempt for each of the two planner and two reviewer holdout cases;
- Qwen `local-plan` and `local-review` profiles with code-defined instructions;
- Qwythos planner `evidence-completeness` instructions with the 1024/1024 generation
  profile;
- Qwythos reviewer `field-checklist` instructions with the deterministic 1536/1536
  profile; and
- the final role-wise qualification thresholds.

A role qualifies only when Qwythos keeps full adapter and schema reliability, keeps every
case at or above `0.6`, gains at least `0.05` role mean score over Qwen, does not reduce
strict case success, and introduces no case regression greater than `0.2`.

## One-shot behavior

Before reading the trusted payload, collection validates the clean implementation commit,
active prompt state, model service identity, G3.1 selection report, and frozen protocol.
It then creates an exclusive reservation beneath:

```text
.local-coder/real-task-holdout/receipts/
```

An interrupted or failed model run leaves its reservation in place and cannot be silently
repeated. Completing a subject writes a second receipt bound to the collection hash. A new
holdout protocol and payload are required to repeat a consumed subject.

Each subject run always evaluates all four holdout cases. There is no planner-only,
reviewer-only, or case-subset command.

Reports retain only case identities and hashes, bounded scoring dimensions, failure codes,
latency, available token counts, and summaries. They retain no task, input, oracle,
generated field, final-answer, prompt, or reasoning text.

## Verify before opening holdout

Commit the runner before collection. From the clean commit:

```bash
make real-task-holdout-check
make real-task-corpus-check \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json
```

Confirm the selected development evidence exists:

```text
.local-coder/real-task-evidence/qwythos-prompt-selection-v1.json
```

## Collect Qwen baseline

Run the Qwen `local-coder` server and LiteLLM, then execute:

```bash
make real-task-holdout-collect \
  SUBJECT=baseline \
  ENVIRONMENT=amelia-gtx1660-v1 \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json \
  SELECTION=.local-coder/real-task-evidence/qwythos-prompt-selection-v1.json
```

## Collect Qwythos candidate

Switch llama.cpp to the frozen Qwythos `local-reason` service without changing LiteLLM or
the repository commit, then execute:

```bash
make real-task-holdout-collect \
  SUBJECT=candidate \
  ENVIRONMENT=amelia-gtx1660-v1 \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json \
  SELECTION=.local-coder/real-task-evidence/qwythos-prompt-selection-v1.json
```

## Final comparison

Resolve the two generated reports and compare them:

```bash
BASELINE="$(ls -1t \
  .local-coder/real-task-evidence/baseline-track-g-holdout-v1-*.json | \
  head -n 1)"
CANDIDATE="$(ls -1t \
  .local-coder/real-task-evidence/candidate-track-g-holdout-v1-*.json | \
  head -n 1)"

make real-task-holdout-compare \
  BASELINE="$BASELINE" \
  CANDIDATE="$CANDIDATE" \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json \
  SELECTION=.local-coder/real-task-evidence/qwythos-prompt-selection-v1.json \
  OUTPUT=.local-coder/real-task-evidence/qwythos-holdout-qualification-v1.json
```

The comparison validates both completion receipts, requires the same implementation,
environment, llama.cpp build, context, and slot count, and issues independent planner and
reviewer decisions. `route_activation` remains `null` regardless of the result.

## Final recorded result

The committed reports are preserved under `evidence/track-g/`. Their canonical internal
hashes are validated at runtime before the role activation manifest is accepted.

| Role | Baseline mean | Qwythos mean | Gain | Strict case success | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Planner | 0.5833 | 0.6667 | +0.0833 | 0/2 → 0/2 | qualified |
| Reviewer | 0.6000 | 0.8000 | +0.2000 | 0/2 → 1/2 | qualified |

No holdout case regressed. The final report records `combined_qualified: true`, qualified
roles `planner` and `reviewer`, and `route_activation: null`. That null is intentional: G4
measured and decided; `profiles/qwythos-role-activation-v1.json` performs the later bounded
activation without modifying the frozen report.

This remains a relative four-case qualification under the preregistered policy, not a claim
that every task succeeds. Planner strict case success stayed at zero and reviewer passed one
of two cases. Runtime activation therefore remains role-bounded, serial, auditable, and
reversible rather than replacing all Qwen routes. See [`MODEL_SWITCHING.md`](MODEL_SWITCHING.md).
