# ROADMAP

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active work only

This file is the repository-wide queue for unfinished engineering work. Completed
programmes belong in [`docs/HISTORY.md`](docs/HISTORY.md); detailed implementation plans
belong under [`docs/roadmaps/`](docs/roadmaps/).

## Current direction: improve repository intelligence

The editing, isolation, model-routing, verification, review, campaign, and prompt-deployment
foundations are implemented. Qwythos is qualified and activated for planner and reviewer
calls, while Qwen remains the explorer, orchestrator, implementer, and repairer model.

The next practical bottleneck is repository understanding. The current explorer evidence
adapter extracts at most three filename-looking strings from the task and otherwise returns
an unranked tracked-file list. The existing `search_repository` implementation rereads
tracked files and performs a Python substring scan for each query.

The primary programme is therefore direct integration of established local code-search and
symbol-indexing tools. This is approved capability work, not a research proposal. Validation
should confirm correct integration, current-worktree accuracy, bounded resource use, and no
regressions; it should not repeatedly re-prove that indexed code search is useful.

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
- Qwythos qualification for planner and reviewer with no holdout case regression; and
- qualification-bound role activation with synchronous serial llama.cpp switching.

The concise historical index is [`docs/HISTORY.md`](docs/HISTORY.md).

## P1 — Indexed repository intelligence

**Status:** primary implementation programme

The detailed design and implementation packet lives in
[`docs/roadmaps/INDEXED_REPOSITORY_INTELLIGENCE.md`](docs/roadmaps/INDEXED_REPOSITORY_INTELLIGENCE.md).

Adopt this stack:

- **ripgrep** for structured live search of the current worktree and as the fallback path;
- **Zoekt** for persistent, multi-repository filename, substring, Boolean, and regex search;
- **Universal Ctags** for symbol definitions and symbol-aware ranking;
- **Git** for repository identity, committed snapshots, dirty overlays, and refresh triggers;
- **plocate** or Everything only for operator-side host filename and repository discovery;
- a narrow local-coder **Repository Context Compiler** that routes queries, merges results,
  reads authoritative current bytes, and supplies bounded context to the existing agents.

Implementation order:

1. replace the Python substring scanner with a structured ripgrep backend;
2. add typed search contracts and the Repository Context Compiler;
3. add repository registration plus Zoekt index build, refresh, status, and query commands;
4. add Universal Ctags symbol indexing and exact symbol lookup;
5. replace the explorer's filename-regex/list fallback with compiled repository context;
6. reuse the same compiler with role-specific policies for planner and reviewer where useful;
7. add automatic index refresh after commits, branch changes, and registered-root updates;
8. add operator-side cross-repository and host filename discovery without widening edit scope;
9. add richer LSP/Serena-style read-only relationship queries after the core stack is stable.

The first delivery is complete when the explorer receives ranked filename, content, and
symbol context from the active worktree; dirty and untracked files override committed index
results; the persistent index is rebuildable; and all access remains repository-scoped and
read-only.

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

**Status:** queued after repository intelligence

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

These are not blocked by a new proof programme. They are simply lower-value than the active
repository-intelligence work.

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
