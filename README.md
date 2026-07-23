# local-coder

A hardware-adjusted, fully local coding-agent stack for the GTX 1660 Ti / 8 GiB RAM
machine used to build this repository.

## Architecture

- **llama.cpp** serves Qwen2.5-Coder locally.
- **LiteLLM** provides stable role aliases: `local-fast`, `local-plan`, and
  `local-review`.
- **smolagents CodeAgent** coordinates managed explorer, planner, implementer,
  repairer, and reviewer agents.
- **DSPy** provides typed explorer, planner, implementer, repairer, and reviewer
  programs behind the existing adapters and stable local routes.
- A **validated native editor** remains the only component allowed to apply strict
  exact edits proposed by the DSPy implementer or repairer.
- **Git worktrees** isolate every agentic run.
- **SQLite** records runs, agents, typed DSPy traces, tool calls, artifacts, verification, and metrics.
- **Black, Flake8, pytest, protected tests, and `git diff --check`** remain authoritative.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the complete design,
[ROADMAP.md](ROADMAP.md) for active implementation work, and
[docs/HANDOFF.md](docs/HANDOFF.md) for the completed recursive-improvement baseline.

## Install the agent runtime

Install the orchestrator and DSPy role-program dependencies in the repository
virtual environment:

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
make skills-lint
./local-coder.py run "Implement the task described in this sentence"
```

`make skills-lint` validates the portable Agent Skill packages independently of the core
`make verify` gate.

A run creates a sibling Git worktree and an `agent/...` branch. It never commits, merges,
or deletes the worktree. The JSON result shows the worktree path and verification state.

Inspect the audit trail:

```bash
./local-coder.py runs
./local-coder.py show-run RUN_ID
./local-coder.py analyze-runs --limit 20
```

Then review the preserved worktree independently before committing or merging.

## Export an offline GEPA dataset

Complete typed DSPy traces can be exported from the audit database without starting
model services or changing live programs:

```bash
./local-coder.py export-gepa-dataset \
  --output .local-coder/gepa-datasets/latest
make gepa-dataset-check
```

The exporter opens SQLite read-only, excludes incomplete or protected evaluator
material, groups identical tasks into deterministic train/dev/holdout splits, and
writes a hash-stamped manifest plus JSONL files. See
[docs/GEPA_DATASET.md](docs/GEPA_DATASET.md).

## Validate or run offline GEPA optimization

Validate one role-specific dataset without model calls:

```bash
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/latest \
  --role planner \
  --output .local-coder/gepa-runs/planner-check \
  --dry-run
make gepa-runner-check
```

A real run requires at least two training examples, one development example, and
three distinct tasks for the selected role. It writes an immutable report and
DSPy candidate state, but never activates, promotes, commits, or merges the
candidate. See [docs/GEPA_OPTIMIZATION.md](docs/GEPA_OPTIMIZATION.md).

## Run a trusted task-plan step

Broad requests can be decomposed by a trusted external planner, including a more capable
model, into a strict JSON plan. The local runtime validates and hashes the complete plan,
then executes only one explicitly selected atomic step:

```bash
./local-coder.py validate-plan task-plan.json
./local-coder.py run-plan-step task-plan.json STEP_ID \
  --approve-plan-hash SHA256_FROM_VALIDATE_PLAN
```

Later steps require explicit `--completed-step` attestations for declared dependencies.
The selected step's editable files are enforced as the editor scope. See
[docs/TASK_PLANS.md](docs/TASK_PLANS.md) for the schema and operator procedure.

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
./local-coder.py audit-campaign --help
```

See [docs/RECURSIVE_IMPROVEMENT.md](docs/RECURSIVE_IMPROVEMENT.md) for the complete
explicit-authorization procedure and sandbox guarantees.

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

## Primary actor startup

The primary actor should read `AGENTS.md` first, then `ROADMAP.md`,
`docs/ARCHITECTURE.md`, and `docs/PIPELINE.md`. Consult `docs/HANDOFF.md` when work
relies on the completed recursive-improvement control-plane guarantees. Unit
verification is service-independent:

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

## Run the full live E2E canary

With llama.cpp and LiteLLM already running, use a clean committed checkout:

```bash
make live-e2e
```

The target runs the static verification gates, skill-package checks, all three
LiteLLM route probes, constrained-output probes through llama.cpp and LiteLLM,
and one isolated real editing run against `profiles/live-e2e-canary.txt`. The
strict result also requires audited DSPy backend markers for `ExplorerProgram`,
`PlannerProgram`, `ImplementerProgram`, `RepairerProgram`, and `ReviewerProgram`.
The suite also runs a controlled failing-repository probe that requires the
repairer to apply one native-editor batch and restore deterministic verification.
A successful canary worktree is removed automatically; failures are preserved for
inspection. The compact
shareable result is written to `.local-coder/live-e2e/latest-summary.json`.
Raw verification output remains in SQLite, while model-facing evidence collapses known
third-party DSPy deprecation warnings into a count and preserves unexpected warnings.

Print only the compact result for support or diagnosis:

```bash
make live-e2e-report
```

The constrained-output probes run 20 attempts per endpoint by default. For a
faster diagnostic pass, override the count explicitly:

```bash
LIVE_E2E_ATTEMPTS=3 make live-e2e
```

Preserve a successful canary worktree for manual inspection when needed:

```bash
LIVE_E2E_KEEP_WORKTREE=1 make live-e2e
```

## First planner GEPA experiment

```bash
./local-coder.py collect-gepa-planner-seed
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/planner-seed-v1 \
  --role planner \
  --target-metric-calls 60 \
  --allow-perfect-only \
  --output .local-coder/gepa-runs/planner-seed-v1-bounded
```

## Inert GEPA campaign candidate

Create a bounded `prompt-optimization` campaign, approve its frozen brief, then run
`build-candidate` to register the offline GEPA result as a hash-bound
`prompt_candidate`. It creates no source worktree and performs no activation or
promotion. Build outcomes distinguish an accepted `candidate_ready` instruction from
`candidate_rejected` and `no_improvement`; only the accepted changed candidate may enter
the later paired-evaluation slice. The report preserves approximate metric-call targets,
actual counts, and the hard campaign model-call limit. Known DSPy `prefix` deprecations
are filtered narrowly while unexpected warnings remain visible. See
[`docs/GEPA_CAMPAIGNS.md`](docs/GEPA_CAMPAIGNS.md).
