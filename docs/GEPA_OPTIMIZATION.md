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
  --auto light \
  --seed 0 \
  --output .local-coder/gepa-runs/planner-001
```

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
