# Offline GEPA optimization runner

The GEPA runner validates or optimizes exactly one typed DSPy specialist program from a
hash-verified exported dataset. It is offline operator tooling, not a new runtime or
promotion path.

## Trust boundary

The runner:

- reads an exported dataset and verifies every manifest and JSONL hash;
- selects only the requested role's `train` and `dev` records;
- never uses the dataset's `holdout` split during optimization;
- uses the existing stable LiteLLM aliases for the student and reflection models;
- writes a new immutable report directory and refuses to overwrite it;
- saves a DSPy program state as an inert candidate artifact; and
- never loads that candidate into the live runtime, edits source, commits, merges,
  pushes, or records a promotion decision.

This replay metric compares candidate typed outputs with audited typed outputs. The
historical verification result and reviewer text are returned as GEPA feedback. It is a
prompt-search metric, not the promotion-grade decision by itself. A `candidate_ready`
program state must subsequently pass the separate paired prompt evaluator and an external
`prompt-replay` holdout described in [`PROMPT_HOLDOUT.md`](PROMPT_HOLDOUT.md).

## Evidence status and feature moratorium

The first checked-in optimization corpus is a synthetic sentinel-replacement smoke suite.
The first live planner campaign produced a changed candidate but was rejected after three
external holdout case regressions. This proves the fail-closed evaluation path, not the
value of prompt optimization for real coding work.

The runner remains supported for bounded experiments and defect fixes. Do not add new
selection, repeated-evaluation, or deployment-hardening features until the evidence gate
in [`../ROADMAP.md`](../ROADMAP.md) is met. The next substantive campaign should use the
real-task corpus defined by
[`roadmaps/REAL_TASK_EVIDENCE.md`](roadmaps/REAL_TASK_EVIDENCE.md).

## Dataset readiness

A real optimization fails closed unless the selected role has:

- at least two `train` examples;
- at least one `dev` example; and
- at least three distinct authoritative tasks.

A missing imperfect example or offline holdout example is reported as a warning rather
than silently ignored. `--dry-run` writes the same readiness report without
invoking DSPy programs, contacting model services, or requiring the dataset to be
ready.

## Dry run

```bash
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/latest \
  --role planner \
  --output .local-coder/gepa-runs/planner-check \
  --dry-run
```

The directory contains `report.json` and `manifest.json`. The manifest hashes every
file and binds the report to the source dataset hash, role, and dry-run status.

## Real optimization

With llama.cpp and LiteLLM already running:

```bash
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/latest \
  --role planner \
  --reflection-route local-plan \
  --target-metric-calls 60 \
  --hard-model-call-limit 80 \
  --max-unsafe-proposals 3 \
  --no-improvement-patience 6 \
  --reflection-max-tokens 512 \
  --seed 0 \
  --output .local-coder/gepa-runs/planner-001
```

When neither `--auto` nor `--target-metric-calls` is supplied, the default GEPA target is
60 metric calls. DSPy and GEPA describe this value as approximate, so the report records
both the requested target and any observed overrun. `--hard-model-call-limit` is the
strict campaign-wide ceiling for student and reflection LM calls. It is enforced by a
shared LM wrapper, including DSPy runtime copies. Provider-internal retries remain
outside that count and are identified as such in the report.

The output additionally contains `candidate.json`. The report records the frozen
baseline replay score, GEPA validation scores, metric-call counts, route identities,
budget, and explicit `not_performed` activation and promotion fields.

Choose a new output directory for every run. Existing directories are never replaced.

## Verification evidence compaction

`make verify` stdout and stderr remain complete in SQLite. A separate
`verification_evidence` artifact records:

- parsed pytest pass/fail/error/skip counts;
- known third-party DSPy `prefix` deprecation warning counts;
- unexpected warning counts;
- bounded failure excerpts; and
- the SHA-256 of the complete raw output.

The manager, repairer, reviewer, exported datasets, and live summary receive the compact
rendering. Known dependency warnings therefore remain auditable without repeatedly
consuming model context. Unexpected warnings and actual failures remain visible.

## Focused checks

```bash
make gepa-dataset-check
make gepa-runner-check
make verify
make agent-smoke
```

## First real planner experiment

`collect-gepa-planner-seed` creates the first optimization-ready planner corpus from
real isolated agent runs. The checked-in seed file contains six independent sentinels.
Each run changes exactly one sentinel in its own Git worktree, executes deterministic
verification and review, and records the normal DSPy trace artifacts in SQLite.

The suite task identities are frozen so the dataset exporter assigns exactly three
planner examples to `train`, two to `dev`, and one to the offline `holdout` split. A
suite hash binds the task text, expected file, and split allocation. Collection fails
closed on a dirty base tree, split drift, unexpected files, failed verification, failed
review, or a non-agent worktree branch.

```bash
./local-coder.py collect-gepa-planner-seed \
  --dataset-output .local-coder/gepa-datasets/planner-seed-v1 \
  --report-output .local-coder/gepa-collections/planner-seed-v1
```

Successful worktrees are preserved by default for inspection. Cleanup is an explicit
operator action:

```bash
./local-coder.py collect-gepa-planner-seed \
  --dataset-output .local-coder/gepa-datasets/planner-seed-v1-clean \
  --report-output .local-coder/gepa-collections/planner-seed-v1-clean \
  --cleanup-successful-worktrees
```

The seed corpus contains only successful examples. A real optimization therefore
fails closed by default; an operator must explicitly acknowledge a null-result
experiment with `--allow-perfect-only`:

```bash
./local-coder.py optimize-gepa \
  --dataset .local-coder/gepa-datasets/planner-seed-v1 \
  --role planner \
  --reflection-route local-plan \
  --target-metric-calls 60 \
  --allow-perfect-only \
  --seed 0 \
  --output .local-coder/gepa-runs/planner-seed-v1-bounded
```

The candidate remains inert. Activation and promotion stay `not_performed`. Holdout
examples are excluded from GEPA and scored only after optimization has finished.

## Optimization hygiene and null results

The runner protects small local models and tiny corpora from unbounded reflection:

- a frozen development baseline of `1.0` skips GEPA entirely unless the operator
  supplies `--force-search-perfect-baseline`;
- an approximate metric-call target and a separate hard LM-call limit are recorded;
- repeated non-improving iterations trigger early stopping;
- a compact typed proposer receives bounded feedback summaries instead of task, evidence,
  and generated-output replay blocks;
- blank, unsafe, or unchanged reflection proposals are blocked from becoming new
  candidates, and repeated unsafe proposals stop the search;
- reflection completions default to 512 tokens rather than the normal role limit;
- replay/example scaffolding, oversized instructions, and mechanically repeated lines
  cause the optimized candidate to be rejected; and
- perfect-only training sets require `--allow-perfect-only`.

The report distinguishes `proposed_candidate_changed` from
`selected_candidate_changed`. The compatibility field `candidate_changed` describes the
selected program only. Null results remain explicit through `winning_candidate`,
`candidate_accepted`, `improvement`, `search_performed`, `perfect_baseline`, and
`optimization_outcome`. When GEPA does not strictly improve the development score,
`candidate.json` contains the original program.

After candidate selection, the runner evaluates the baseline and selected candidate on
the role's offline holdout split. The holdout records are never supplied to GEPA, its
reflection model, or candidate selection. The report records
`exposed_during_optimization: false`, score deltas, and separate metric-call accounting.
