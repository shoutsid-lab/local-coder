# Project History

**Status:** Historical index. This file is not an active work queue or required reading for
routine changes.

The current implementation direction lives in [`../ROADMAP.md`](../ROADMAP.md). This file
keeps a concise map of completed programmes and the evidence they actually established.

## Completed programmes

### Tracks A–B — Agent Skills and typed role programs

Delivered portable skill discovery and activation, stable LiteLLM route aliases, and typed
DSPy adapters for explorer, planner, implementer, repairer, and reviewer roles.

### Tracks C–D — Bounded improvement and prompt deployment

Delivered audited dataset export, bounded GEPA candidate construction, paired development
and external holdout evaluation, independent decisions, campaign close and audit, and
promotion-bound prompt activation and rollback.

Detailed retained references:

- [`HANDOFF.md`](HANDOFF.md) — control-plane completion record;
- [`RECURSIVE_IMPROVEMENT.md`](RECURSIVE_IMPROVEMENT.md) — closed programme summary;
- [`VALIDATION_HISTORY.md`](VALIDATION_HISTORY.md) — evidence and retained controls;
- [`GEPA_CAMPAIGNS.md`](GEPA_CAMPAIGNS.md) — operator campaign workflow; and
- [`PROMPT_DEPLOYMENT.md`](PROMPT_DEPLOYMENT.md) — activation and rollback boundary.

## Evidence interpretation

The completed work proves that local-coder can constrain edits, isolate runs, preserve
audit lineage, reject tampered or regressing candidates, and prevent rejected prompt
states from becoming active.

It does **not** prove that prompt optimization improves real coding capability. The first
live planner campaign produced a changed candidate with better aggregate scores but
regressed three external holdout cases and was correctly rejected. The original GEPA seed
corpus is synthetic and remains suitable for smoke coverage, not primary capability claims.

The strategic response is to retain the control plane while freezing further hardening
until stronger evidence exists. Active work now prioritizes reasoning-capable routes and a
real-task benchmark corpus.

## Trust-model interpretation

Candidate-neutral approvals, holdout isolation, and activation gates protect against
accidental lineage errors, malformed outputs, external candidates, implementation bugs,
and future stronger models. They are forward-looking integrity controls; the repository
does not claim that the current 3B model is a sophisticated adversary.

## Documentation lifecycle

Living documents are the README, root roadmap, architecture, pipeline, and conventions.
Detailed operator references remain active only while their subsystem is supported.
Completed programme narratives belong here or in the linked retained records and should
not return to the active reading path.
