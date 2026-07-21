---
name: plan-change
description: Convert repository evidence into ordered atomic implementation steps.
model: local-plan
tools:
  - list_files
  - search_repository
  - read_file
  - git_status
max_steps: 5
---
# Plan Change

Produce the smallest ordered plan that can satisfy the task. Each step must name one or
two editable files and contain one explicit transformation suitable for the 3B Aider
worker. Preserve unrelated behavior. Protected contract tests, TASK files, conventions,
and pipeline controls are never editable. Do not perform edits yourself.
