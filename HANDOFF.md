# Codex Handoff

## Current state

The repository baseline is GitHub `shoutsid-lab/local-coder` `main` at commit
`8f12ea1f78fd692017797591cf1ee1948b8d7b1d`. `UPSTREAM.json` records the verified
baseline blob IDs. The agent-runtime upgrade is designed to be committed as one coherent
change on top of that baseline.

The surrounding local architecture remains fixed; the user explicitly authorized
replacing the flaky editing worker:

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

The native editor in `runtime/editor.py` is the implementation worker. It asks
`local-fast` for strict JSON exact replacements, validates the complete batch before
writing, and never stages or commits. Deterministic verification remains authoritative.

## Verified before handoff

- GitHub baseline alignment recorded in `UPSTREAM.json`.
- Python formatting, linting, compilation, JSON validation, and unit tests pass.
- Five skills load with their expected model and tool boundaries.
- The smolagents CodeAgent manager, two read-only evidence adapters, one fixed
  read-only review adapter, and two code-action managed workers instantiate with
  version 1.26.
- Worktree creation shares the base repository virtual environment.
- Tracked, staged, and untracked worktree changes are included in final diff review.
- The native editor rejects protected or unapproved paths, ambiguous and missing exact
  matches, no-op edits, non-UTF-8 files, and oversized context before writing.
- Direct `./local-coder.py` execution re-enters `.venv`, preventing a false
  `smolagents NOT INSTALLED` result.

## Bounded live validation

The first live end-to-end task completed successfully against the local llama-server and
LiteLLM services. Because the upgrade was still uncommitted and the runtime correctly
rejects a dirty base repository, the exact working-tree diff was committed only inside a
disposable validation clone. The source repository and validation worktree remained
uncommitted.

The earlier task replaced one exact sentence in `README.md`. Its trajectory included
explorer and planner reads, one source edit, diff inspection, deterministic verification,
and read-only semantic review. Only `README.md` changed; all 25 tests passed; the review
verdict was `pass`; and the run ended as `awaiting_approval`.

Follow-up regression exposed prompt-example leakage in the former editing worker: it
repeatedly created and staged `mathweb/flask/app.py`, then hid that staged empty file from
the original diff collector. The runtime correctly detected the scope violation after a
new postcondition was added, but the worker remained too flaky. At the user's direction,
it has been removed entirely. The replacement has no prompt examples, Git integration,
or ability to create paths; its response schema enumerates the only approved files.

The same regression also hardened diff collection to compare against `HEAD`, so staged
changes cannot escape review, and made recorded scope violations deterministically force
`needs_attention` even if semantic review returns `pass`.

The replacement editor then completed a fresh bounded end-to-end regression from a
committed disposable clone. Run `d3720aea52f0` replaced one exact standalone line in
`README.md`. The native editor returned one fenced JSON edit, the parser normalized the
fence, and the path-enumerated schema plus exact-match validator applied only that edit.
All 34 tests passed in the worktree, the fixed read-only reviewer returned `pass`, and the
run ended as `awaiting_approval`. The final worktree contained only the unstaged
one-line `README.md` diff. SQLite recorded six agents, fourteen successful tool calls,
and four verification results with no failed tool calls.

A post-commit bounded regression exposed one more small-model boundary: the managed
reviewer attempted unavailable write operations after inspecting the diff. The reviewer
is now a fixed read-only adapter with no code executor. It deterministically gathers the
diff and verification evidence and invokes the existing semantic reviewer, so write
operations cannot be generated or attempted by that role.

This proves the native bounded exact-edit path. It does not prove broad autonomous
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
2. Repeat the native bounded regression from the committed source repository and
   preserve its run record for comparison.
3. Add one bounded multi-edit regression to prove all edits validate before writes.
4. Improve review explanations without weakening strict verdict validation.
5. Only then consider an on-demand deeper model route, MCP integrations, or offline
   prompt optimisation.

Do not return to synthetic calculator fault injection unless it is needed for a specific
regression test.
