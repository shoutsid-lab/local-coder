---
name: review-change
description: Review the final branch diff without editing files. Use after deterministic verification to assess task fit, scope, and definite semantic issues.
compatibility: Requires read-only diff inspection and deterministic verification results.
---
# Review Change

Work read-only. Inspect the final diff and deterministic verification results, then assess
whether the change satisfies the task and remains within scope. Use a semantic review
capability when the client provides one.

Summarize definite issues separately from judgement calls. Never edit, commit, merge,
push, or approve a failing change.

Use the [review checklist](references/REVIEW_CHECKLIST.md) for a portable structured
assessment.
