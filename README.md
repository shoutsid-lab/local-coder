# local-coder

A hardware-adjusted, fully local coding-agent stack for the GTX 1660 Ti / 8 GiB RAM
machine used to build this repository.

## Architecture

- **llama.cpp** serves Qwen2.5-Coder locally.
- **LiteLLM** provides stable role aliases: `local-fast`, `local-plan`, and
  `local-review`.
- **smolagents CodeAgent** coordinates managed explorer, planner, implementer,
  repairer, and reviewer agents.
- **Aider** remains the only source-editing worker and receives narrowly scoped edits.
- **Git worktrees** isolate every agentic run.
- **SQLite** records runs, agents, tool calls, artifacts, verification, and metrics.
- **Black, Flake8, pytest, protected tests, and `git diff --check`** remain authoritative.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete design.

## Install the agent runtime

The existing calculator/pipeline environment remains unchanged. Add the orchestrator
dependency to the repository virtual environment:

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
```

Then review the preserved worktree manually before committing or merging.

## Existing fallback commands

The proven lower-level commands remain available:

```bash
./local-coder.py task FILE [FILE ...]
./local-coder.py repair "ATOMIC INSTRUCTION" FILE [FILE ...]
./local-coder.py plan
./local-coder.py execute
./local-coder.py verify
./local-coder.py review
```

## Upstream alignment

`UPSTREAM.json` records the GitHub repository, commit, and verified blob SHAs used as the
baseline before the agent-runtime changes were applied.

## Codex handoff

Codex should read `AGENTS.md` first, then `HANDOFF.md`, `ARCHITECTURE.md`, and
`PIPELINE.md`. Unit verification is service-independent:

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
