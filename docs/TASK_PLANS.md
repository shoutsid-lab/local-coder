# Trusted Task Plans

Task plans let a human or stronger external planner decompose a broad request without
asking the local 3B model to invent or execute an unbounded workflow. The local runtime
validates the complete plan read-only, freezes its canonical SHA-256, and runs only one
explicitly selected step at a time.

The workflow does not commit, merge, push, promote, delete worktrees, or automatically
advance to another step.

## Plan schema

```json
{
  "schema_version": 1,
  "plan_id": "parser-cleanup-v1",
  "objective": "Improve parser error reporting in two reviewed steps.",
  "steps": [
    {
      "id": "implementation",
      "instruction": "Clarify the parser error message without changing behavior.",
      "editable_files": ["runtime/parser.py"],
      "acceptance_criteria": [
        "The error identifies the invalid token.",
        "Existing parser behavior remains unchanged."
      ],
      "depends_on": []
    },
    {
      "id": "tests",
      "instruction": "Add a regression test for the clarified error message.",
      "editable_files": ["tests/test_parser.py"],
      "acceptance_criteria": [
        "The regression test passes deterministically."
      ],
      "depends_on": ["implementation"]
    }
  ]
}
```

The validator requires exact fields, one to twelve ordered steps, one or two approved
existing UTF-8 files per step, unique identifiers, nonempty acceptance criteria, and
backward-only dependencies. Protected files, path traversal, duplicate paths, oversized
context, malformed JSON, and edits to the plan file itself fail closed.

## Operator flow

Validate the complete plan and inspect the returned hash:

```bash
./local-coder.py validate-plan task-plan.json
```

Run one selected step only after approving that exact hash:

```bash
./local-coder.py run-plan-step task-plan.json implementation \
  --approve-plan-hash SHA256_FROM_VALIDATE_PLAN
```

After inspecting and manually committing an accepted dependency, attest it when selecting
a later step:

```bash
./local-coder.py run-plan-step task-plan.json tests \
  --approve-plan-hash SHA256_FROM_VALIDATE_PLAN \
  --completed-step implementation
```

Each run still creates one isolated worktree and records its actual baseline commit. The
selected step's `editable_files` become an enforced editor scope for every managed role;
a model request for any other path is rejected before the editor is called and forces
`needs_attention`.

## Trust boundary

The plan is human-authored input, not model authority. Hash approval detects changes after
review, but it is not a signature. Dependency completion is a deliberate human attestation;
the runtime does not infer it, chain worktrees, or perform Git promotion actions. Continue
to inspect every worktree, deterministic verification result, diff, and fresh review before
committing.
