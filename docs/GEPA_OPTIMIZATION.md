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
foundation for prompt search, not a promotion-grade evaluator; campaign integration and
trusted holdout scoring remain later work.

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
  --max-metric-calls 60 \
  --no-improvement-patience 6 \
  --reflection-max-tokens 512 \
  --seed 0 \
  --output .local-coder/gepa-runs/planner-001
```

When neither `--auto` nor `--max-metric-calls` is supplied, the bounded default is 60
metric calls. `--auto light|medium|heavy` remains available as an explicit operator
choice, but its DSPy-compatible budget is converted to a concrete metric-call ceiling
so the same early-stop guard remains active.

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
  --max-metric-calls 60 \
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
- a concrete metric-call ceiling is always active;
- repeated non-improving iterations trigger early stopping;
- reflection completions default to 512 tokens rather than the normal role limit;
- replay/example scaffolding, oversized instructions, and mechanically repeated lines
  cause the optimized candidate to be rejected; and
- perfect-only training sets require `--allow-perfect-only`.

The report makes null results explicit with `winning_candidate`, `candidate_changed`,
`candidate_accepted`, `improvement`, `search_performed`, `perfect_baseline`, and
`optimization_outcome`. When GEPA does not strictly improve the development score,
`candidate.json` contains the original program.

After candidate selection, the runner evaluates the baseline and selected candidate on
the role's offline holdout split. The holdout records are never supplied to GEPA, its
reflection model, or candidate selection. The report records
`exposed_during_optimization: false`, score deltas, and separate metric-call accounting.
