---
name: plan-change
description: Convert repository evidence into ordered atomic implementation steps. Use after exploration when a task must be decomposed for the constrained local editor.
compatibility: Requires repository evidence supplied by a read-only exploration step.
---
# Plan Change

Work only from the supplied repository evidence. Produce the plan as concise plain text;
do not perform edits or emit executable tool calls.

Produce the smallest ordered plan that can satisfy the task. Each step must name one or
two editable files and contain one explicit transformation suitable for a constrained
exact editor. Preserve unrelated behavior. Protected contract tests, task files,
conventions, and pipeline controls are never editable.

Use the [portable plan format](references/PLAN_FORMAT.md) when cross-testing this skill in
another client.
