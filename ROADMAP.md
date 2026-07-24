# ROADMAP

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active work only

This file is the repository-wide queue for unfinished engineering work. Completed
programmes belong in [`docs/HISTORY.md`](docs/HISTORY.md); detailed implementation plans
belong under [`docs/roadmaps/`](docs/roadmaps/).

## Current direction: close routing, then stabilize state

Indexed repository intelligence is implemented. Explorer and Planner now receive ranked,
bounded current-worktree context from ripgrep, persistent Zoekt indexes, Universal Ctags,
and Git-aware overlays. Repository registration remains read-only and external indexes are
disposable.

The next active item is the target-machine closeout for reasoning-route switching. State
schema stabilization follows it.

## Completed foundation

The following capabilities are complete and retained:

- portable Agent Skill discovery, activation, packaging, and linting;
- typed DSPy programmes for explorer, planner, implementer, repairer, and reviewer;
- validated exact editing, isolated Git worktrees, deterministic verification, and review;
- bounded source and prompt campaigns with independent authorization;
- paired development and external holdout evaluation;
- prompt activation, hash-verified loading, and rollback;
- reasoning-aware response contracts and route probes;
- a frozen real-task development corpus and consumed external holdout;
- Qwythos qualification for planner and reviewer with no holdout case regression;
- qualification-bound role activation with synchronous serial llama.cpp switching; and
- indexed repository intelligence with current-byte ripgrep, persistent Zoekt, Universal
  Ctags symbols, Git overlays, repository discovery, and bounded Explorer/Planner context.

The concise historical index is [`docs/HISTORY.md`](docs/HISTORY.md). The retained
implementation record is
[`docs/roadmaps/INDEXED_REPOSITORY_INTELLIGENCE.md`](docs/roadmaps/INDEXED_REPOSITORY_INTELLIGENCE.md).

## P2 — Close reasoning-route integration

**Status:** implementation complete; target-machine closeout pending

Track F F0–F5 are implemented. The remaining closeout is operational rather than
architectural:

- run one live Qwen → Qwythos → Qwen switch cycle on the target machine;
- run one bounded local-coder task that exercises planner/reviewer switching;
- record the resulting service identities, route calls, shutdown behaviour, and final model;
- move the completed Track F roadmap to history after the live cycle passes.

Detailed reference:
[`docs/roadmaps/REASONING_MODEL_ROUTES.md`](docs/roadmaps/REASONING_MODEL_ROUTES.md).

F6 MTP comparison is optional tuning and does not block Track F closure.

## P3 — State schema stabilization

**Status:** queued after reasoning-route closeout

Plan one explicit pre-stable schema reset rather than retaining every development migration
indefinitely.

- Export campaign or validation records worth retaining.
- Define one current SQLite schema and a supported reset procedure.
- Decide whether pre-stable local databases are recreated rather than upgraded.
- Remove obsolete compatibility branches only after the reset boundary is documented and
  tested.
- Keep stable releases migration-compatible after that reset point.

**Completion condition:** one documented schema baseline, a deterministic reset/export
path, and no accidental promise to support every development database shape forever.

## P4 — MCP operator transport

**Status:** queued

The detailed plan remains in
[`docs/roadmaps/MCP_CONTROL_PLANE.md`](docs/roadmaps/MCP_CONTROL_PLANE.md).

MCP remains optional operator transport. It should follow repository intelligence so its
read-only tools can expose the improved search and context surfaces rather than the current
substring scanner. MCP does not replace the internal Python contracts or widen source-write
authority.

## Deferred maintenance and optional work

Resume these only for a concrete defect, an active promoted prompt, or an explicit task:

- post-activation health checks, interrupted-write recovery, drift detection, and automatic
  prompt rollback;
- regression-aware prompt-candidate selection and repeated replay stability work;
- additional GEPA campaign kinds or selection machinery;
- MTP performance comparison;
- cross-runtime publication of portable role skills.

These are not blocked by a new proof programme. They are simply lower-value than the
reasoning-route closeout and state-schema work above.

## Permanent constraints

- Keep llama.cpp, LiteLLM, and stable logical routes `local-fast`, `local-plan`,
  `local-review`, and `local-reason`.
- Keep the native exact editor as the only agent source-writing boundary.
- Keep Git worktrees for run isolation and SQLite for audit lineage.
- Do not add automatic source commit, merge, push, or destructive worktree cleanup.
- Do not expose candidate-visible holdout material or candidate-controlled evaluation.
- Do not make the core local loop depend on a cloud service.
- Keep resource-intensive work bounded for the current hardware.
- Repository search scope and edit scope are separate capabilities; searching another
  registered repository must never grant authority to edit it.

## Definition of done for roadmap items

Run the checks applicable to the changed subsystem. Documentation-only changes require at
least `git diff --check`; runtime changes normally require:

```bash
make verify
make agent-smoke
make skills-lint
git diff --check
```

Use focused subsystem checks when their code changes. Do not run every historical campaign
or qualification suite for unrelated work.
