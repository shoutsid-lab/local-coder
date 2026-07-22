---
name: atomic-implementation
description: Apply one narrowly scoped source change through the validated native editor. Use when an approved implementation step requires exact edits to one or two existing files.
compatibility: Requires an exact-edit capability, diff inspection, and deterministic verification.
---
# Atomic Implementation

Use the client's validated exact-edit capability for every edit. Give it one explicit
transformation at a time and only the approved existing files. Reject ambiguous edits
unless every search and replacement is unique and the complete batch can be validated
before writing.

Inspect the diff immediately and run deterministic verification after each completed
atomic change. Do not describe an edit without performing it. Never edit tests merely to
make a failure disappear, and never commit, merge, or push changes.

Use the [exact-edit safety contract](references/EXACT_EDIT_CONTRACT.md) when mapping this
workflow to another client's tools.
