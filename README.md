# local-coder

A hardware-adjusted, fully local coding-agent stack for the GTX 1660 Ti / 8 GiB RAM
machine used to build this repository.

## Architecture

- **llama.cpp** serves Qwen2.5-Coder locally.
- **LiteLLM** provides stable role aliases: `local-fast`, `local-plan`, and
  `local-review`.
- **smolagents CodeAgent** coordinates managed explorer, planner, implementer,
  repairer, and reviewer agents.
- A **validated native editor** converts narrow instructions into strict exact edits.
- **Git worktrees** isolate every agentic run.
- **SQLite** records runs, agents, tool calls, artifacts, verification, and metrics.
- **Black, Flake8, pytest, protected tests, and `git diff --check`** remain authoritative.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the complete design and
[HANDOFF.md](HANDOFF.md) for the recursive-improvement roadmap.

## Install the agent runtime

Install the orchestrator dependency in the repository virtual environment:

```bash
make agent-install
```

## Start the local services

Check whether the existing llama-server on port 8080 and LiteLLM proxy on port 4000
are already running first. If either one is down, start it, then check:

```bash
./local-coder.py status
```

## Run the role-separated harness

```bash
./local-coder.py skills
./local-coder.py run "Implement the task described in this sentence"
```

A run creates a sibling Git worktree and an `agent/...` branch. It never commits, merges,
or deletes the worktree. The JSON result shows the worktree path and verification state.

Inspect the audit trail:

```bash
./local-coder.py runs
./local-coder.py show-run RUN_ID
./local-coder.py analyze-runs --limit 20
```

Then review the preserved worktree manually before committing or merging.

## Run a recursive-improvement campaign

The trusted evaluator can mine one bounded brief, compare clean committed generations,
and recommend—but never perform—promotion:

```bash
./local-coder.py create-campaign --help
./local-coder.py approve-brief --help
./local-coder.py build-candidate --help
./local-coder.py evaluate --help
./local-coder.py record-decision --help
./local-coder.py close-campaign --help
```

See [docs/RECURSIVE_IMPROVEMENT.md](docs/RECURSIVE_IMPROVEMENT.md) for the complete
human-gated procedure and sandbox guarantees.

## Focused fallback commands

The proven lower-level commands remain available:

```bash
./local-coder.py repair "ATOMIC INSTRUCTION" FILE [FILE ...]
./local-coder.py verify
./local-coder.py review TASK_FILE
```

## Upstream alignment

`docs/UPSTREAM.json` records the GitHub repository, commit, and verified blob SHAs used as the
baseline before the agent-runtime changes were applied.

## Codex handoff

Codex should read `AGENTS.md` first, then `HANDOFF.md`, `docs/ARCHITECTURE.md`, and
`docs/PIPELINE.md`. Unit verification is service-independent:

```bash
make verify
make agent-smoke
```

A complete handoff check additionally requires a committed, clean tree:

```bash
make handoff-check
```

Direct `./local-coder.py` invocations automatically re-execute inside `.venv` when the
project virtual environment exists.
