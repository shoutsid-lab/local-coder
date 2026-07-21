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

The first committed-source regression then completed from a clean disposable clone of
`cc3668b`. Run `43bc88984ee8` made one literal sentence replacement in `README.md`,
ended as `awaiting_approval`, and received a `pass` verdict from the fixed reviewer. The
preserved worktree contained only the requested unstaged one-line diff, with no staged or
untracked files. All 35 tests passed. SQLite recorded six agents, twelve successful tool
calls, and four passing verification results with no failed tool calls.

A deterministic multi-edit regression now exercises the runtime's `request_and_apply`
entrypoint with a valid first replacement and an invalid second replacement across two
approved files. The complete batch fails validation before the write loop, and both files
remain unchanged. This directly proves the fail-before-write boundary for generated
multi-edit batches; all 36 tests pass.

Semantic-review explanations are now hardened without relaxing verdict validation. The
review prompt requires all four fields and a concrete, verification-grounded summary;
the parser rejects unknown verdicts, blank or oversized explanations, and invalid list
items. A verdict-only small-model response remains compatible but receives a deterministic
fallback naming the changed files and verification result. A live `local-review` call
returned `pass` with a concrete behavioral explanation, and all 38 tests pass.

A second committed-source trajectory, run `9bf491ef79c7`, requested two literal
`README.md` replacements in one edit batch. The manager instead split the work into two
delegations. The first implementer applied both replacements through two successful editor
calls; the redundant second delegation then accumulated seven safely rejected exact-match
attempts. Five verification runs and semantic review passed, and the final source diff was
correct, but the old status logic returned `awaiting_approval`. Inspection also found the
shared `.venv` directory symlink as an untracked path that the diff renderer had skipped.
This trajectory reinforces the current 3B decomposition boundary and does not justify a
deeper route or broader integration yet.

The runtime is now hardened from that evidence. Any rejected `apply_atomic_edit` call
forces `needs_attention` even when verification and semantic review pass, and the result
reports the rejected-attempt count. The expected `.venv` symlink is ignored cleanly, while
any other untracked symbolic link is rendered explicitly for review. Deterministic tests
cover both postconditions; all 40 tests pass.

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

1. After committing the trajectory hardening, repeat the bounded two-replacement task and
   confirm that `.venv` stays ignored and rejected editor attempts force `needs_attention`.
2. Keep the current model routes and integration surface unchanged until clean bounded
   trajectories justify expanding them.

Do not return to synthetic calculator fault injection unless it is needed for a specific
regression test.
