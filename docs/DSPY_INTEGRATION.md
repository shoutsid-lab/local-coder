# DSPy Integration

## Scope

DSPy is an internal role-program layer behind the existing smolagents adapters. It does
not replace orchestration, worktree isolation, the native editor, deterministic
verification, or the read-only reviewer boundary.

The first migrated role is the reviewer because it cannot edit files and has the lowest
blast radius. Explorer, planner, implementer, and repairer remain on their existing
adapters until separate reviewed changes migrate them one at a time.

## LM wiring

`runtime/dspy_lm.py` constructs `dspy.LM` instances against the existing LiteLLM OpenAI-
compatible endpoint:

| Trusted route | DSPy model identifier | Current role |
|---|---|---|
| `local-fast` | `openai/local-fast` | reserved for implementer and repairer |
| `local-plan` | `openai/local-plan` | reserved for explorer and planner |
| `local-review` | `openai/local-review` | reviewer |

The factory fixes temperature to zero, disables DSPy response caching for fresh audit
evidence, and permits only the three existing route aliases. No new inference service or
cloud credential is introduced.

## Reviewer program

`runtime/dspy_programs/reviewer.py` defines:

- `ReviewerSignature`, a typed contract from authoritative task, changed files, Git diff,
  and deterministic verification evidence to verdict, summary, issues, and unrelated
  changes;
- `ReviewerProgram`, a single-step `dspy.Predict` module suitable for the current 3B
  model profile;
- JSONAdapter execution with per-call token usage tracking.

`review-diff.py` remains the fixed read-only adapter. It still collects the Git diff,
runs `make verify`, validates the final verdict fields, writes `REVIEW.json`, and exposes
only the same CLI contract. The only change is how the semantic verdict content is built
and parsed internally.

## Audit and rollback

Reviewer model metrics use source `dspy-reviewer` and record `ReviewerProgram` plus
`JSONAdapter`. `make live-e2e` requires that marker in addition to the existing
`local-review` route, successful verification, exact file scope, and a passing verdict.

The migration is independently revertible: the reviewer adapter can switch back to the
previous direct request implementation without changing smolagents, the native editor,
worktrees, SQLite schema, or verification gates.
