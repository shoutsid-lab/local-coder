# Exact-edit safety contract

An implementation client should preserve these invariants even when its tool names differ:

- limit each operation to the approved existing files;
- describe one exact transformation at a time;
- reject ambiguous searches or replacements that match more than once;
- validate the complete edit batch before writing any file;
- inspect the resulting diff immediately; and
- run deterministic verification before reporting completion.

The editing capability must not commit, merge, push, or weaken tests and safety controls.
