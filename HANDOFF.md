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
- The smolagents CodeAgent manager, two read-only evidence adapters, and three
  code-action managed workers instantiate with version 1.26.
- Worktree creation shares the base repository virtual environment.
- Tracked and untracked worktree changes are included in final diff review.
- `run-aider.sh apply` supports multiple sequential atomic edits and defers full
  verification to the orchestrator.
- Direct `./local-coder.py` execution re-enters `.venv`, preventing a false
  `smolagents NOT INSTALLED` result.

## Bounded live validation

The first live end-to-end task completed successfully against the local llama-server and
LiteLLM services. Because the upgrade was still uncommitted and the runtime correctly
rejects a dirty base repository, the exact working-tree diff was committed only inside a
disposable validation clone. The source repository and validation worktree remained
uncommitted.

The task replaced one exact sentence in `README.md`. The recorded trajectory included
explorer and planner reads, one Aider edit, diff inspection, deterministic verification,
and read-only semantic review. Only `README.md` changed; all 25 tests passed; the review
verdict was `pass`; and the run ended as `awaiting_approval`. SQLite recorded six agent
registrations, twelve tool calls, four artifacts, and four verification results.

The validation exposed and fixed three additional boundaries: Aider could report success
without changing an editable file, the reviewer script was invoked as an executable even
though it is a Python source file, and the 3B reviewer sometimes returned only a valid
verdict rather than the full requested object. Aider now uses search/replace diffs with a
strict changed-file postcondition, review runs through the project interpreter, and a
verdict-only response is normalized into the stored schema with an explicit note that
explanatory details were omitted.

This proves the bounded, exact-edit path. It does not prove broad autonomous
decomposition. The 3B model still requires atomic tasks with explicit file paths and
literal before/after text where practical.

## First local validation after committing

```bash
make verify
make agent-smoke
./local-coder.py status
make handoff-check
```

`make handoff-check` intentionally requires a clean working tree, so run it only after
committing the upgrade.

For a post-commit regression, exercise another real, bounded task:

```bash
./local-coder.py status
./local-coder.py run "Implement one narrowly scoped task with clear acceptance criteria"
```

If either service is down, start the existing llama-server on port 8080 and LiteLLM
proxy on port 4000 first, then rerun the status check.

Inspect the returned worktree, run record, diff, verification output, and review verdict.
Do not merge automatically.

## Current model boundary

All three logical roles currently route to the same Qwen2.5-Coder-3B model. This is a
valid hardware-adjusted starting point, but broad autonomous decomposition remains the
main risk. Keep edits atomic. A future 7B planning/review profile should be loaded on
demand rather than concurrently, and only after real 3B trajectories justify it.

## Next meaningful work

1. Commit this upgrade and obtain a clean handoff check.
2. Repeat the bounded regression from the committed repository and preserve its run
   record for comparison.
3. Improve review explanations without weakening strict verdict validation.
4. Only then consider an on-demand deeper model route, MCP integrations, or offline
   prompt optimisation.

Do not return to synthetic calculator fault injection unless it is needed for a specific
regression test.
