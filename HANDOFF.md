# Codex Handoff

## Current state

The repository baseline is GitHub `shoutsid-lab/local-coder` `main` at commit
`8f12ea1f78fd692017797591cf1ee1948b8d7b1d`. `UPSTREAM.json` records the verified
baseline blob IDs. The agent-runtime upgrade is designed to be committed as one coherent
change on top of that baseline.

The architecture is intentionally fixed:

```text
Codex or developer
   ↓
local-coder role-separated harness
   ├── focused skills
   ├── managed explorer/planner/implementer/repairer/reviewer agents
   ├── narrow tools
   ├── Git worktree isolation
   └── SQLite trajectory logging
        ↓
LiteLLM logical routes
        ↓
llama.cpp local inference
```

Aider remains the implementation worker. Deterministic verification remains authoritative.

## Verified before handoff

- GitHub baseline alignment recorded in `UPSTREAM.json`.
- Python formatting, linting, compilation, JSON validation, and unit tests pass.
- Five skills load with their expected model and tool boundaries.
- The smolagents manager and all five managed agents instantiate with version 1.26.
- Worktree creation shares the base repository virtual environment.
- Tracked and untracked worktree changes are included in final diff review.
- `run-aider.sh apply` supports multiple sequential atomic edits and defers full
  verification to the orchestrator.
- Direct `./local-coder.py` execution re-enters `.venv`, preventing a false
  `smolagents NOT INSTALLED` result.

## Important honest limitation

A live end-to-end multi-agent implementation has not yet completed after this upgrade.
The attempted run stopped before creating a worktree because the upgrade files had not
been committed and the base repository was dirty. This is expected safety behaviour.
Do not report the live agentic path as production-proven until a real task completes in a
clean repository with llama-server and LiteLLM running.

## First local validation after committing

```bash
make verify
make agent-smoke
./local-coder.py status
make handoff-check
```

`make handoff-check` intentionally requires a clean working tree, so run it only after
committing the upgrade.

Then exercise one real, bounded task:

```bash
./local-coder.py run "Implement one narrowly scoped task with clear acceptance criteria"
```

Inspect the returned worktree, run record, diff, verification output, and review verdict.
Do not merge automatically.

## Current model boundary

All three logical roles currently route to the same Qwen2.5-Coder-3B model. This is a
valid hardware-adjusted starting point, but broad autonomous decomposition remains the
main risk. Keep edits atomic. A future 7B planning/review profile should be loaded on
demand rather than concurrently, and only after real 3B trajectories justify it.

## Next meaningful work

1. Commit this upgrade and obtain a clean handoff check.
2. Complete one real end-to-end task and inspect the SQLite trajectory.
3. Fix failures exposed by that run without changing the architecture.
4. Only then consider an on-demand deeper model route, MCP integrations, or offline
   prompt optimisation.

Do not return to synthetic calculator fault injection unless it is needed for a specific
regression test.
