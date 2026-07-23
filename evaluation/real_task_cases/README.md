# Track G real-task cases

This directory contains the candidate-visible portion of the frozen Track G corpus.

- `development-v1.json` contains eight complete planner/reviewer cases derived from real
  repository work, failures, and accepted fixes.
- `holdout-v1.index.json` contains only metadata and SHA-256 identities for four final
  holdout cases. It intentionally contains no task text, model inputs, successful outcome,
  or oracle.

The trusted holdout payload is provisioned separately at:

```text
.local-coder/real-task-holdout/holdout-v1.json
```

That path is ignored by Git and must never be mounted into candidate worktrees, copied into
prompts, or included in development reports. The committed index binds every hidden case
and the complete holdout suite by canonical JSON hash.

Validate the committed development suite and holdout index with:

```bash
make real-task-corpus-check
```

After separately installing the trusted holdout payload, bind it to the committed index:

```bash
make real-task-corpus-check \
  HOLDOUT=.local-coder/real-task-holdout/holdout-v1.json
```

This G0/G1 corpus freezes case inputs and oracles. It performs no model calls. G2 and G3
consume these identities for current-route baselines and shared-adapter comparisons.
