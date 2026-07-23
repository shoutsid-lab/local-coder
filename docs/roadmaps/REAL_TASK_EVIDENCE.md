# ROADMAP: Real-Task Capability Evidence

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active — runs alongside Track F
**Track:** G

## 0. Why this document exists

The repository has extensive deterministic tests for its control plane but limited evidence
about end-to-end coding capability. The existing GEPA seed corpus contains synthetic
sentinel replacements, and the first live optimized planner candidate was rejected by its
external holdout.

Track G builds evidence from actual repository work before more optimization or deployment
hardening is funded. It answers a narrower question:

> Which planner/reviewer route combination most reliably completes real, bounded coding
> tasks on this hardware?

This programme reuses existing worktrees, exact editing, verification, review, and audit
surfaces. It does not add a new campaign kind or evaluation authority.

## 1. Corpus contract

Each benchmark case must contain:

- an immutable repository baseline commit or archived tree identity;
- a concrete task statement available to the tested planner;
- allowed or expected scope where the historical task had one;
- deterministic verification commands;
- the known successful outcome or patch for scoring and diagnosis;
- provenance explaining why the case represents real work; and
- redacted secrets and machine-specific paths.

Primary cases must come from actual work, including:

- defects previously fixed in this repository;
- lint, formatting, and contract failures found during real handoffs;
- stale-baseline or patch-application failures;
- JSON/stdout boundary defects;
- route and reasoning-response failures;
- small multi-file documentation consistency changes;
- bounded runtime bugs with regression tests; and
- failed agent attempts whose final human-assisted fix is known.

Synthetic sentinel replacements may remain as smoke fixtures but do not count toward the
primary benchmark size or promotion evidence.

## 2. Initial corpus target

Freeze an initial set of at least 12 cases across these classes:

| Class | Minimum |
| --- | ---: |
| Exact one-file repair | 3 |
| Test or lint failure repair | 2 |
| Multi-file bounded change | 2 |
| Planning or evidence-selection failure | 2 |
| Reviewer defect detection | 2 |
| Documentation or interface consistency | 1 |

Avoid selecting only tasks already encoded in current role prompts. Keep a final holdout
subset unavailable during route tuning.

## 3. Comparison matrix

Keep explorer, implementer, repairer, editor, verification, and task inputs fixed. Compare:

1. current planner + current reviewer;
2. `local-reason` planner + current reviewer;
3. current planner + `local-reason` reviewer; and
4. `local-reason` planner + `local-reason` reviewer.

Qualify planner and reviewer independently. A model may be accepted for one role and
rejected for the other. The tested route must not select itself from task text or model
output.

## 4. Metrics

Record at least:

- task completion and deterministic verification success;
- valid plan or review schema rate;
- final-answer completion rate and reasoning-only truncations;
- files touched outside expected scope;
- edit rejection and repair iteration counts;
- reviewer true-positive and false-positive behavior;
- wall time, startup/switch time, and generated tokens;
- prompt, completion, and available reasoning-token accounting;
- model and route identity; and
- per-case results, not only aggregate means.

A route cannot qualify by improving the mean while introducing material case regressions
without an explicit, frozen tradeoff decision.

## 5. Phased delivery

### G0. Case format and collector

- Define a small versioned manifest format.
- Add a validator and deterministic case loader.
- Document how to archive a baseline without secrets or generated state.
- Add no model calls in this phase.

### G1. Historical case collection

- Convert at least 12 real tasks into frozen cases.
- Preserve task provenance and successful outcomes.
- Separate development and final holdout cases before route qualification.
- Review the corpus manually for leakage and duplicate task patterns.

### G2. Current-route baseline

- Run the current planner/reviewer combination through the corpus.
- Establish completion, schema, latency, token, and repair baselines.
- Record failure clusters without changing prompts during the run.

### G3. Track F comparison

- Run the frozen comparison matrix with bounded route profiles.
- Use identical task inputs and verification commands.
- Decide planner and reviewer qualification independently.
- Keep the final holdout unavailable during route tuning.

### G4. Decision and backlog reset

Use the evidence to decide:

- whether `local-reason` improves planner and/or reviewer outcomes;
- whether model switching cost is acceptable;
- which failure classes should drive ordinary engineering work;
- whether another real GEPA campaign is justified; and
- whether frozen control-plane items R1–R3 should remain frozen.

## 6. Exit criteria

Track G is complete when:

- at least 12 real cases are frozen with provenance and deterministic checks;
- the current route baseline is reproducible;
- planner/reviewer route combinations are compared on the same cases;
- per-case and aggregate outcomes are recorded;
- final holdout evidence remains independent; and
- the root roadmap is updated from measured bottlenecks rather than architectural
  preference.

## 7. Non-goals

- No new autonomous benchmark generator.
- No model training or prompt optimization against the final holdout.
- No replacement of deterministic verification with model scoring.
- No requirement that a reasoning model wins.
- No new campaign kind, authorization layer, or deployment mechanism.
- No claim that synthetic smoke fixtures measure real coding capability.

## 8. Deliverables

Expected deliverables include:

- a versioned real-task manifest and validator;
- an ignored local archive location for case baselines;
- at least 12 reviewed case manifests;
- a bounded runner that reuses existing local-coder commands;
- a machine-readable comparison report;
- a concise qualification decision for planner and reviewer; and
- updates to [`../VALIDATION_HISTORY.md`](../VALIDATION_HISTORY.md) and the root roadmap.

## 9. Succession

Track G shares the capability milestone with Track F. After both programmes complete, use
an unused descriptive programme identifier; Track H is the next available letter if a
lettered track is helpful.
