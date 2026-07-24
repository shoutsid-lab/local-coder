# Project History

**Status:** Historical index. This file is not an active work queue or required reading for
routine changes.

The current implementation direction lives in [`../ROADMAP.md`](../ROADMAP.md). This file
keeps a concise map of completed programmes and the capabilities they established.

## Completed programmes

### Tracks A–B — Agent Skills and typed role programmes

Delivered portable skill discovery and activation, stable LiteLLM route aliases, and typed
DSPy adapters for explorer, planner, implementer, repairer, and reviewer roles.

### Tracks C–D — Bounded improvement and prompt deployment

Delivered audited dataset export, bounded GEPA candidate construction, paired development
and external holdout evaluation, independent decisions, campaign close and audit, and
promotion-bound prompt activation and rollback.

Detailed retained references:

- [`HANDOFF.md`](HANDOFF.md) — control-plane completion record;
- [`RECURSIVE_IMPROVEMENT.md`](RECURSIVE_IMPROVEMENT.md) — closed programme summary;
- [`VALIDATION_HISTORY.md`](VALIDATION_HISTORY.md) — retained validation record;
- [`GEPA_CAMPAIGNS.md`](GEPA_CAMPAIGNS.md) — operator campaign workflow; and
- [`PROMPT_DEPLOYMENT.md`](PROMPT_DEPLOYMENT.md) — activation and rollback boundary.

### Track G — Real-task corpus and route qualification

Track G froze a versioned corpus from actual repository tasks: eight development cases and
a separately consumed four-case holdout. Development work selected role-specific Qwythos
prompt contracts, and the one-shot holdout qualified Qwythos for planner and reviewer with
no case-level regression.

The result is role-specific. Explorer, orchestration, implementation, and repair remain on
Qwen. The normalized reports remain under `evidence/track-g/` and are retained rather than
regenerated.

References:

- [`roadmaps/REAL_TASK_EVIDENCE.md`](roadmaps/REAL_TASK_EVIDENCE.md);
- [`REAL_TASK_CORPUS.md`](REAL_TASK_CORPUS.md);
- [`QWYTHOS_PROMPT_TUNING.md`](QWYTHOS_PROMPT_TUNING.md); and
- [`QWYTHOS_HOLDOUT_QUALIFICATION.md`](QWYTHOS_HOLDOUT_QUALIFICATION.md).

## Completed foundation within active Track F

Reasoning-aware response normalization, route probes, qualification-bound planner/reviewer
profiles, and synchronous serial llama.cpp switching are implemented. Unknown live servers
fail closed, failed profile loads restore the prior recognized profile, and Qwen is restored
after Qwythos specialist calls.

Track F remains in the active roadmap only for one target-machine switch cycle and one
bounded end-to-end run. See [`roadmaps/REASONING_MODEL_ROUTES.md`](roadmaps/REASONING_MODEL_ROUTES.md)
and [`MODEL_SWITCHING.md`](MODEL_SWITCHING.md).

## Interpretation

The completed work establishes a strong local execution and control foundation. It does not
make further capability work conditional on creating another campaign, holdout, or extensive
comparison programme. Established infrastructure may be integrated directly with focused
regression, resource, and operational checks.

The active capability priority is indexed repository intelligence: better filename, text,
regex, symbol, and cross-repository localisation for the existing role-separated agents.

## Documentation lifecycle

Living documents are the README, root roadmap, architecture, pipeline, and conventions.
Detailed operator references remain active only while their subsystem is supported.
Completed programme narratives belong here or in the linked retained records and should not
return to the active reading path.
