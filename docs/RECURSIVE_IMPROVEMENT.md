# Recursive Improvement Programme — Complete

**Status:** Complete through prompt deployment and rollback.

This document is closed as an implementation programme. New engineering work belongs only
in [`../ROADMAP.md`](../ROADMAP.md). The detailed control-plane completion record remains
in [`HANDOFF.md`](HANDOFF.md).

## Delivered capability

`local-coder` now provides one bounded, auditable improvement lifecycle for source and
prompt candidates:

```text
historical evidence
    ↓
approved bounded brief
    ↓
isolated source candidate or inert prompt candidate
    ↓
paired development and external holdout evaluation
    ↓
ordered scorecard
    ↓
independent authorization decision
    ↓
campaign close and read-only audit
    ↓
optional prompt activation or rollback
```

The candidate under evaluation cannot approve its own brief, control the evaluator,
access external holdout oracle data, record its own promotion decision, or activate
itself.

## Stable boundaries

- Source candidates use isolated Git worktrees and the validated native exact editor.
- Prompt candidates are inert, hash-bound DSPy program states.
- Development and external holdout inputs are frozen independently.
- Evaluation uses ordered safety, correctness, regression, control, improvement, and
  efficiency gates rather than a scalar tradeoff.
- Model-call, prompt-token, completion-token, process, memory, time, disk, file, and
  output limits fail closed.
- SQLite stores campaign, build, evaluator, artifact, scorecard, decision, activation,
  and rollback lineage.
- Source promotion remains outside the runtime; the system does not commit, merge, or
  push candidate code.
- Prompt activation is a separate trusted action allowed only after an eligible
  scorecard, explicit `promote` decision, clean close, and successful audit.
- Active prompt states are copied to trusted storage, hash-verified before loading,
  atomically replaced, and independently reversible.

## Operator references

| Activity | Reference |
| --- | --- |
| Source-candidate campaign and control-plane guarantees | [`HANDOFF.md`](HANDOFF.md) |
| GEPA dataset export | [`GEPA_DATASET.md`](GEPA_DATASET.md) |
| Direct offline optimization | [`GEPA_OPTIMIZATION.md`](GEPA_OPTIMIZATION.md) |
| Prompt candidate construction and paired evaluation | [`GEPA_CAMPAIGNS.md`](GEPA_CAMPAIGNS.md) |
| External prompt holdout schema | [`PROMPT_HOLDOUT.md`](PROMPT_HOLDOUT.md) |
| Prompt activation and rollback | [`PROMPT_DEPLOYMENT.md`](PROMPT_DEPLOYMENT.md) |
| Historical evidence behind controls | [`VALIDATION_HISTORY.md`](VALIDATION_HISTORY.md) |

## Core campaign commands

```bash
./local-coder.py analyze-runs --limit 20
./local-coder.py rotate-holdout ROTATION_ID \
  --manifest /external/path/manifest.json \
  --oracle /external/path/oracle.json
./local-coder.py create-campaign --help
./local-coder.py approve-brief --help
./local-coder.py build-candidate --help
./local-coder.py evaluate --help
./local-coder.py record-decision --help
./local-coder.py close-campaign --help
./local-coder.py audit-campaign --help
```

Prompt campaigns can complete their post-build lifecycle through:

```bash
scripts/run-prompt-lifecycle.sh \
  CAMPAIGN_ID \
  BUILD_ID \
  /external/path/manifest.json \
  /external/path/oracle.json \
  "ACTOR" \
  --activate
```

A valid rejection is finalized and audited without activation. Omit `--activate` to
complete decision, close, and audit while leaving runtime behavior unchanged.

## Completion evidence

The delivered programme has demonstrated:

- deterministic campaign creation, approval, build lineage, paired evaluation,
  scorecard, decision, close, and audit;
- explicit prompt build outcomes: `candidate_ready`, `candidate_rejected`, and
  `no_improvement`;
- shared hard LM-call and token accounting across student and reflection routes;
- clean JSON-only command output with third-party progress routed to stderr;
- BaseLM-compatible budget wrappers under the installed DSPy runtime;
- external prompt holdout evaluation with redacted oracle evidence;
- correct rejection of a candidate that improved aggregate scores while regressing
  individual holdout cases; and
- refusal to activate that rejected candidate during the unattended deployment
  lifecycle.

The final programme verification reached 231 passing tests in the project environment,
including focused GEPA, prompt-campaign, prompt-evaluation, and prompt-deployment suites.

## Programme closure

The programme is complete at the bounded scope described above. Future deployment safety,
prompt-selection robustness, repeated evaluation, optional reflection capacity, and
skills ecosystem work are tracked only in [`../ROADMAP.md`](../ROADMAP.md).
