# Offline GEPA dataset export

`local-coder` exports typed DSPy role traces from the existing SQLite audit trail.
The exporter is a dataset-preparation boundary only: it does not run GEPA, contact a
model service, modify live role programs, or participate in promotion decisions.

## Audit source

Each successful typed role invocation appends a `dspy_trace` artifact containing:

- the fixed role, program, adapter, and LiteLLM route identity;
- the exact bounded inputs supplied to the DSPy program;
- the validated typed output returned by the program; and
- non-authoritative adapter metadata such as native-editor or repair verification
  results.

The exporter opens `.local-coder/state/agent.db` in SQLite read-only mode. A trace is
eligible only when it has a matching successful DSPy backend metric, a deterministic
verification result, and a structured reviewer verdict. Incomplete or malformed rows
are counted in the manifest and excluded rather than guessed. Complete raw verification
output remains in SQLite; the dataset stores compact hash-bound evidence with test
counts, warning counts, and bounded failure details.

## Export

```bash
./local-coder.py export-gepa-dataset \
  --output .local-coder/gepa-datasets/latest
```

Select exact runs when reproducing a prior dataset:

```bash
./local-coder.py export-gepa-dataset \
  --run-id RUN_ID \
  --run-id ANOTHER_RUN_ID \
  --output .local-coder/gepa-datasets/reproduction
```

The output directory contains:

- `manifest.json` with deterministic content hashes, source run IDs, exclusion counts,
  and role/split counts;
- `examples.jsonl` with every eligible example; and
- `train.jsonl`, `dev.jsonl`, and `holdout.jsonl` deterministic splits.

All examples from identical authoritative task text are assigned to the same split.
The split is derived from `sha256(task)`, so rerunning the exporter against unchanged
audit records produces byte-identical files.

## Leakage boundary

The exporter rejects any example whose trace, verification output, or reviewer feedback
references trusted evaluator paths under:

- `evaluation/holdout/`;
- `evaluation/oracles/`; or
- `.local-coder/holdout/`.

The exported `holdout.jsonl` is only an offline dataset split. It is not the secret
campaign holdout and contains no evaluator oracle data.

## DSPy conversion

`runtime.dspy_programs.gepa_dataset.to_dspy_examples` converts verified JSONL records
into `dspy.Example` objects with `task`, `role`, and `evidence` as inputs. The expected
output, deterministic pass/fail result, reviewer verdict, textual feedback, and scalar
score remain labels for a later offline optimizer runner.

No optimized program is loaded by the runtime in this phase. Exported records may
contain repository source and task text, so keep them local and treat them as sensitive
audit material. The separate offline runner is documented in
[GEPA_OPTIMIZATION.md](GEPA_OPTIMIZATION.md).

## Verification

```bash
make gepa-dataset-check
make verify
```

The focused check covers read-only access, deterministic hashes and splits, malformed
record exclusion, protected holdout/oracle rejection, tamper detection, and DSPy
Example conversion.
