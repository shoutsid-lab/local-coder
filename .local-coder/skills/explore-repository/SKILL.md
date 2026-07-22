---
name: explore-repository
description: Inspect repository structure and locate the smallest relevant code surface. Use before planning a change when files, symbols, tests, or conventions must be identified without editing.
compatibility: Requires read-only repository listing, search, and file-reading capabilities.
---
# Explore Repository

The read-only adapter must use only repository evidence supplied by the client or
gathered through read-only capabilities. Locate the files, symbols, tests, and conventions
that govern the task. Return a concise evidence-backed summary for the planner.

Work read-only; do not emit code or editing instructions. Do not suggest broad refactors, edit files, or
read large files in full when a focused range is enough.

Use the [evidence checklist](references/EVIDENCE_CHECKLIST.md) when another client needs a
portable output contract.
