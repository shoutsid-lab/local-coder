# ROADMAP: Real-Task Capability Evidence

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Complete — G0–G4 measured; qualified activation moved to Track F
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

## 2.1 Frozen v1 split

| Split | Planner | Reviewer | Total | Candidate-visible content |
| --- | ---: | ---: | ---: | --- |
| Development | 4 | 4 | 8 | Full inputs and trusted development oracle |
| Holdout | 2 | 2 | 4 | Metadata and hashes only |
| **Total** | **6** | **6** | **12** | — |

The committed holdout index contains no task, input, successful-outcome, or oracle fields.
The full payload is installed separately under
`.local-coder/real-task-holdout/holdout-v1.json` and must not be mounted into candidate
worktrees or prompts.

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

### G0. Case format and collector — complete

- `evaluation/real_task_corpus.py` defines the strict versioned format, loader, canonical
  hashes, coverage checks, split checks, and holdout binding.
- [`../REAL_TASK_CORPUS.md`](../REAL_TASK_CORPUS.md) documents baseline identities,
  candidate-visible controls, trusted holdout installation, and validation commands.
- The phase performs no model calls.

### G1. Historical case collection — complete

- `evaluation/real_task_cases/development-v1.json` freezes eight complete real-task cases.
- `evaluation/real_task_cases/holdout-v1.index.json` freezes metadata and hashes for four
  independently provisioned holdout cases without exposing their tasks or oracles.
- The combined corpus has six planner and six reviewer cases and meets every class minimum
  in section 2.
- Pattern groups are unique across development and holdout, machine-specific paths are
  rejected, and trusted holdout tampering fails closed.

### G2. Current-route baseline — complete

- `evaluation/real_task_development.py` runs the current planner/reviewer combination
  through all eight candidate-visible development cases using the production DSPy
  programs and `JSONAdapter`.
- `profiles/track-g-development-v1.json` binds the suite hash, one attempt per case, the
  role-oracle scorer, both model identities, and exact route profiles.
- Every case records adapter success, schema adherence, role-specific oracle dimensions,
  bounded failure codes, latency, and available prompt/completion tokens.
- Reports retain no generated planner/reviewer text and expose no holdout path.
- Active prompt lineage is sampled before and after collection; any change fails the run.
- Clean-tree Qwen and Qwythos reports now exist for the same eight cases, implementation
  commit, adapters, scoring, and environment. Qwythos improved overall mean score from
  0.775 to approximately 0.821 and strict case success from two to three, but also regressed
  on two cases and used materially more tokens and wall time.
- The mixed development result is not sufficient to open holdout or change a route.
  Downstream edit/repair counts remain a later end-to-end measurement.

### G3. Accuracy-first Qwythos profile comparison — measured

- `profiles/track-g-qwythos-tuning-v1.json` froze `current-control`,
  `deterministic-accuracy`, and `role-depth-accuracy`, with two attempts per visible case.
- `current-control` retained the highest mean planner and overall scores, but one 0.5 planner
  attempt failed the frozen 0.6 minimum-case hard gate. The eligible planner profiles tied
  on accuracy; `role-depth-accuracy` won only the latency tie-break.
- All reviewer profiles tied at 0.9 mean score. `deterministic-accuracy` improved stable
  reviewer case success to 0.75 and used fewer tokens and wall time.
- No selected role gained 0.02 mean score over control. The combined eligible projection
  also remained below the frozen overall threshold, so holdout stayed sealed.
- Increasing reasoning depth did not remove the repeated planner acceptance-criteria,
  instruction-completeness, and scope-reference failures.

### G3.1. Qwythos prompt-contract comparison — measured

- `profiles/track-g-qwythos-prompt-tuning-v1.json` froze `code-control`,
  `evidence-completeness`, and `field-checklist` while holding generation settings fixed.
- `evidence-completeness` raised planner mean score from 0.75 to 0.8125 and the minimum
  attempt score from 0.5 to approximately 0.667.
- `field-checklist` raised reviewer mean score from 0.9 to 0.95 while retaining 0.75 stable
  case success and raising the minimum score from 0.6 to 0.8.
- Neither selected role introduced a material case regression. Both cleared the frozen
  role-wise holdout gate; the combined development gate remained closed only because stable
  case success did not increase.
- Further tuning against the eight visible cases stops here to avoid case-specific
  overfitting.

### G4. One-shot holdout qualification — complete

- `profiles/track-g-holdout-qualification-v1.json` binds the sealed suite and index hashes,
  G3.1 comparison hash, exact Qwen and Qwythos role configurations, one attempt per case,
  and final qualification thresholds.
- `evaluation/real_task_holdout.py` validates all non-holdout controls before creating an
  exclusive subject reservation and loading the trusted payload.
- Each subject always runs both planner and reviewer cases; partial roles and case subsets
  are unsupported.
- Interrupted runs remain consumed, completed runs receive hash-bound receipts, and final
  comparison requires both completion receipts.
- Planner and reviewer qualify independently. No evidence result activates a route.
- The one-shot collection completed on 2026-07-24. Qwythos qualified for both roles with no
  case-level regression: planner mean improved from 0.5833 to 0.6667 and reviewer mean
  improved from 0.6 to 0.8.
- The baseline, candidate, and final comparison are retained under `evidence/track-g/`; the
  activation manifest validates their canonical hashes before changing role routes.

Track G is complete. Track F owns the separate role activation and serial switching layer.
The result supports planner/reviewer promotion only; it does not promote implementation or
repair, and it does not reopen the consumed holdout for tuning.

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
