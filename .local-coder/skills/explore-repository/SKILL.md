---
name: explore-repository
description: Inspect repository structure and locate the smallest relevant code surface. Use before planning a change when files, symbols, tests, or conventions must be identified without editing.
---
# Explore Repository

The read-only adapter gathers repository evidence before invoking the model. Use only
that supplied evidence; do not emit code, tool calls, or editing instructions.

Work read-only. Locate the files, symbols, tests, and conventions that govern the task.
Return a concise evidence-backed summary for the planner. Do not suggest broad refactors,
do not edit files, and do not read large files in full when a focused range is enough.
