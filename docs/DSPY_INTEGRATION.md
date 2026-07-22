# DSPy Integration

## Scope

DSPy is an internal role-program layer behind the existing smolagents adapters. It does
not replace orchestration, worktree isolation, the native editor, deterministic
verification, or the read-only reviewer boundary.

The reviewer was migrated first because it cannot edit files and has the lowest blast
radius. Explorer and planner now also use typed DSPy programs behind their existing
read-only evidence adapters. Implementer and repairer remain on their existing
smolagents/native-editor path until separate reviewed changes migrate them last.

## LM wiring

`runtime/dspy_lm.py` constructs `dspy.LM` instances against the existing LiteLLM OpenAI-
compatible endpoint:

| Trusted route | DSPy model identifier | Current role |
|---|---|---|
| `local-fast` | `openai/local-fast` | reserved for implementer and repairer |
| `local-plan` | `openai/local-plan` | explorer and planner |
| `local-review` | `openai/local-review` | reviewer |

The factory fixes temperature to zero, disables DSPy response caching for fresh audit
evidence, and permits only the three existing route aliases. No new inference service or
cloud credential is introduced.

## Explorer and planner programs

`runtime/dspy_programs/explorer.py` defines a typed `ExplorerSignature` from the
authoritative task, delegated request, and bounded repository evidence to findings,
relevant files, constraints, and unresolved questions.

`runtime/dspy_programs/planner.py` defines a typed `PlannerSignature` from the same
read-only evidence boundary to one atomic instruction, one or two existing editable
files, observable acceptance criteria, and backward-only dependencies. Both programs use
`dspy.ChainOfThought` internally and `dspy.JSONAdapter` at the adapter boundary. Their
manager-facing text is rendered deterministically only after validating bounded lists
and safe repository-relative paths.

The existing evidence adapter still performs all repository reads, activates the
portable skill lazily, exposes no tools to DSPy, and records model usage in SQLite. DSPy
cannot edit files or expand the editor allowlist.

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

Read-only role metrics use sources `dspy-explorer`, `dspy-planner`, and
`dspy-reviewer`, recording their program name plus `JSONAdapter`. `make live-e2e`
requires all three markers in addition to the existing logical routes, successful
verification, exact file scope, and a passing verdict.

Each migration is independently revertible: a read-only adapter can switch back to its
previous direct request implementation without changing smolagents orchestration, the
native editor, worktrees, SQLite schema, or verification gates.
