---
name: review-change
description: Review the final branch diff without editing files.
model: local-review
tools:
  - read_file
  - inspect_diff
  - run_verification
  - review_diff
  - git_status
max_steps: 5
---
# Review Change

The read-only adapter calls `inspect_diff`, `run_verification`, and `review_diff` in a
fixed sequence. It does not expose a code executor or any editing operation.

Work read-only. Confirm the diff satisfies the task, remains within scope, and is supported
by deterministic verification. Use the semantic reviewer tool, then summarize definite
issues separately from judgement calls. Never edit, commit, or approve a failing change.
