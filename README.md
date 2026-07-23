# local-coder

A hardware-adjusted, fully local coding-agent stack built for a GTX 1660-class GPU
and 8 GiB of system memory.

`local-coder` combines role-separated agents, typed DSPy programs, isolated Git
worktrees, deterministic verification, and an audited improvement control plane. The
runtime stays local-first: llama.cpp serves the model, LiteLLM provides stable logical
routes, and SQLite records execution and evaluation evidence.

## System overview

| Layer | Responsibility |
| --- | --- |
| llama.cpp | Serves Qwen2.5-Coder locally. |
| LiteLLM | Exposes `local-fast`, `local-plan`, and `local-review`. |
| smolagents | Coordinates explorer, planner, implementer, repairer, and reviewer roles. |
| DSPy | Provides typed role programs and loadable prompt states. |
| Native editor | Applies validated exact replacements to approved files. |
| Git worktrees | Isolate agent runs and preserve uncommitted results for review. |
| SQLite | Stores runs, traces, artifacts, verification, campaigns, and decisions. |
| Trusted evaluator | Runs paired checks, holdout gates, audits, and prompt deployment controls. |

Black, Flake8, pytest, protected tests, and `git diff --check` remain authoritative.
The model does not decide whether its own changes pass.

## Install

Install the agent runtime and DSPy dependencies in the repository virtual environment:

```bash
make agent-install
```

Direct `./local-coder.py` commands automatically re-execute inside `.venv` when it
exists.

## Start the local services

Start llama.cpp on port 8080 and LiteLLM on port 4000, then verify both routes:

```bash
./local-coder.py status
```

## Run the coding harness

Inspect the available role skills and run one bounded task:

```bash
./local-coder.py skills
make skills-lint
./local-coder.py run "Implement one atomic task"
```

Each run creates a sibling worktree and an `agent/...` branch. The runtime does not
commit, merge, push, or remove the worktree.

Inspect recorded evidence:

```bash
./local-coder.py runs
./local-coder.py show-run RUN_ID
./local-coder.py analyze-runs --limit 20
```

Review the preserved worktree before committing or merging anything.

## Run a trusted task-plan step

A trusted external planner can provide a strict JSON plan. The runtime hashes and
validates the complete plan, then executes only the explicitly selected atomic step:

```bash
./local-coder.py validate-plan task-plan.json
./local-coder.py run-plan-step task-plan.json STEP_ID \
  --approve-plan-hash SHA256_FROM_VALIDATE_PLAN
```

Later steps require explicit completion attestations for declared dependencies. See
[`docs/TASK_PLANS.md`](docs/TASK_PLANS.md).

## Verify the repository

Run service-independent verification:

```bash
make verify
make agent-smoke
```

A complete clean-tree handoff check is available after committing documentation or code
changes:

```bash
make handoff-check
```

Focused verification targets include:

```bash
make skills-lint
make gepa-dataset-check
make gepa-runner-check
make prompt-campaign-check
make prompt-deployment-check
```

## Prompt optimization and deployment

Prompt work follows one audited lifecycle. Candidate construction, paired evaluation,
authorization, and activation remain separate operations.

### 1. Export an offline dataset

```bash
./local-coder.py export-gepa-dataset \
  --output .local-coder/gepa-datasets/latest
make gepa-dataset-check
```

The exporter opens SQLite read-only, excludes protected evaluator material, groups
identical tasks deterministically, and writes a hash-bound manifest with JSONL splits.
See [`docs/GEPA_DATASET.md`](docs/GEPA_DATASET.md).

### 2. Validate or run GEPA directly

Validate a role-specific dataset without model calls:

```bash
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/latest \
  --role planner \
  --output .local-coder/gepa-runs/planner-check \
  --dry-run
```

A real run writes an immutable report and inert DSPy candidate state. It does not
activate, promote, commit, or merge anything. See
[`docs/GEPA_OPTIMIZATION.md`](docs/GEPA_OPTIMIZATION.md).

The first planner seed corpus can be collected and optimized with:

```bash
./local-coder.py collect-gepa-planner-seed
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/planner-seed-v1 \
  --role planner \
  --target-metric-calls 60 \
  --allow-perfect-only \
  --output .local-coder/gepa-runs/planner-seed-v1-bounded
```

### 3. Run an audited prompt campaign

```bash
./local-coder.py create-campaign --help
./local-coder.py approve-brief --help
./local-coder.py build-candidate --help
./local-coder.py evaluate --help
./local-coder.py record-decision --help
./local-coder.py close-campaign --help
./local-coder.py audit-campaign --help
```

A `prompt-optimization` build produces one of three explicit outcomes:
`candidate_ready`, `candidate_rejected`, or `no_improvement`. Only a changed,
`candidate_ready` state can enter paired development and external holdout evaluation.
See [`docs/GEPA_CAMPAIGNS.md`](docs/GEPA_CAMPAIGNS.md) and
[`docs/PROMPT_HOLDOUT.md`](docs/PROMPT_HOLDOUT.md).

### 4. Finalize, activate, or roll back

Complete an evaluated campaign without changing runtime behavior:

```bash
./local-coder.py finalize-prompt-campaign CAMPAIGN_ID \
  --actor "ACTOR" \
  --rationale "Decision derived from the frozen scorecard."
```

Add `--activate` only when the scorecard, decision, close, and audit all permit
promotion. Inspect or roll back active states with:

```bash
./local-coder.py show-active-prompts
./local-coder.py rollback-prompt ROLE \
  --actor "ACTOR" \
  --rationale "Runtime regression observed after activation."
```

The candidate cannot activate itself. Active states are copied into trusted storage,
hash-verified before loading, and replaced atomically. See
[`docs/PROMPT_DEPLOYMENT.md`](docs/PROMPT_DEPLOYMENT.md).

## Focused fallback commands

```bash
./local-coder.py repair "ATOMIC INSTRUCTION" FILE [FILE ...]
./local-coder.py verify
./local-coder.py review TASK_FILE
```

## Run the live E2E canary

With llama.cpp and LiteLLM running, use a clean committed checkout:

```bash
make live-e2e
```

The canary checks skills, static verification, all logical routes, constrained JSON
responses, one isolated editing run, and one controlled repair trajectory. Successful
worktrees are removed; failed worktrees are preserved. The compact result is written to
`.local-coder/live-e2e/latest-summary.json`.

```bash
make live-e2e-report
LIVE_E2E_ATTEMPTS=3 make live-e2e
LIVE_E2E_KEEP_WORKTREE=1 make live-e2e
```

## Documentation map

| Document | Purpose |
| --- | --- |
| [`ROADMAP.md`](ROADMAP.md) | Active and deferred engineering work only. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component and trust boundaries. |
| [`docs/PIPELINE.md`](docs/PIPELINE.md) | Editing, verification, review, and approval flow. |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Completed recursive-improvement control-plane record. |
| [`docs/RECURSIVE_IMPROVEMENT.md`](docs/RECURSIVE_IMPROVEMENT.md) | Closed programme summary and operating references. |
| [`docs/GEPA_DATASET.md`](docs/GEPA_DATASET.md) | Audited dataset export. |
| [`docs/GEPA_OPTIMIZATION.md`](docs/GEPA_OPTIMIZATION.md) | Direct offline optimization runner. |
| [`docs/GEPA_CAMPAIGNS.md`](docs/GEPA_CAMPAIGNS.md) | Prompt campaign construction and evaluation. |
| [`docs/PROMPT_HOLDOUT.md`](docs/PROMPT_HOLDOUT.md) | External holdout format and isolation. |
| [`docs/PROMPT_DEPLOYMENT.md`](docs/PROMPT_DEPLOYMENT.md) | Promotion-bound activation and rollback. |
| [`docs/VALIDATION_HISTORY.md`](docs/VALIDATION_HISTORY.md) | Historical evidence behind retained controls. |

Primary actors should read `AGENTS.md`, `ROADMAP.md`, `docs/ARCHITECTURE.md`, and
`docs/PIPELINE.md` before changing the repository.
