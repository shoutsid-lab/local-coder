---
name: plan-change
description: Convert repository evidence into ordered atomic implementation steps.
model: local-plan
tools:
  - list_files
  - search_repository
  - read_file
  - git_status
max_steps: 1
---
# Plan Change

The read-only adapter gathers repository evidence before invoking the model. Produce the
plan as plain text only; do not emit code, tool calls, or editing instructions.

Produce the smallest ordered plan that can satisfy the task. Each step must name one or
two editable files and contain one explicit transformation suitable for the 3B native
editor. Preserve unrelated behavior. Protected contract tests, TASK files, conventions,
and pipeline controls are never editable. Do not perform edits yourself.
