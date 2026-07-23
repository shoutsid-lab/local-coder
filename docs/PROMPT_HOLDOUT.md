# External prompt holdout format

Paired prompt evaluation uses an operator-controlled holdout that is separate from the
GEPA training, development, and offline holdout splits. The manifest contains only model
inputs. Expected typed outputs remain in a separate oracle file and are never emitted by
the evaluation CLI.

## Manifest

```json
{
  "schema_version": 1,
  "suite_kind": "prompt-replay",
  "suite_id": "planner-external-v1",
  "visibility": "holdout",
  "role": "planner",
  "cases": [
    {
      "id": "planner-unseen-001",
      "inputs": {
        "task": "Plan one unseen repository change.",
        "delegated_task": "Return one bounded implementation step.",
        "repository_evidence": ["src/example.py contains the relevant behavior."]
      }
    }
  ]
}
```

Each case must provide exactly the input fields for the selected DSPy role. Case IDs must
be unique and must not overlap with development example IDs.

## Oracle

```json
{
  "schema_version": 1,
  "suite_kind": "prompt-replay",
  "suite_id": "planner-external-v1",
  "role": "planner",
  "cases": {
    "planner-unseen-001": {
      "output": {
        "instruction": "Update src/example.py to implement the requested behavior.",
        "editable_files": ["src/example.py"],
        "acceptance_criteria": ["The requested behavior is covered deterministically."],
        "depends_on": []
      }
    }
  }
}
```

The manifest and oracle must have identical suite IDs, roles, and case IDs. Oracle outputs
must match the exact typed output fields for the role.

## Provision the rotation

Keep the source files outside candidate-visible Git content, then provision an immutable
copy under ignored trusted storage:

```bash
./local-coder.py rotate-holdout planner-external-v1 \
  --manifest /operator/path/planner-manifest.json \
  --oracle /operator/path/planner-oracle.json
```

The command reports `suite_kind: prompt-replay` and the two immutable paths under
`.local-coder/holdout/`.

## Evaluate an inert candidate

```bash
./local-coder.py evaluate \
  --campaign-id CAMPAIGN_ID \
  --build-id BUILD_ID \
  --holdout-suite .local-coder/holdout/planner-external-v1/manifest.json \
  --holdout-oracle .local-coder/holdout/planner-external-v1/oracle.json
```

Prompt evaluation does not accept `--baseline`, `--candidate`, `--allowed-file`, or
`--target-case`. Baseline and candidate instruction hashes come from the approved prompt
campaign and its hash-bound `prompt_candidate` artifact.

The evaluator replays the frozen development split and external holdout against both
program states. It uses the existing ordered scorecard gates:

1. candidate safety and inertness;
2. typed-output correctness;
3. no external holdout regression;
4. hard model-call, token-accounting, and lineage control;
5. strict development improvement; and
6. bounded evaluation efficiency.

Holdout oracle outputs, exact per-case scores, and observation hashes are redacted from
stdout. Baseline and candidate routes share the frozen model-call, prompt-token, and
completion-token limits. Missing usage data, blocked calls, or any exhausted limit fails
closed at the control boundary. The candidate program state and evaluator identity are
archived with verified hashes in SQLite. New campaigns freeze that evaluator hash at
creation; older C2.1 campaigns bind it exactly once before their first evaluation.
Evaluation only recommends promotion. `record-decision` remains a separate action, and
neither evaluation nor decision recording activates the prompt.
