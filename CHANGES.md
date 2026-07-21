# Agent Runtime Upgrade

This package starts from GitHub `shoutsid-lab/local-coder` at commit
`8f12ea1f78fd692017797591cf1ee1948b8d7b1d` and preserves the existing LiteLLM,
planner, executor, reviewer, and verification architecture. Aider was replaced by a
validated native exact-edit worker after live validation exposed prompt-example leakage.

Added:

- `runtime/` role-separated smolagents harness
- five `.local-coder/skills/*/SKILL.md` procedures
- isolated worktree orchestration
- narrow repository/editor/verification/review tools
- SQLite trajectory and audit storage
- fast and future deep llama.cpp profiles
- `local-coder.py run`, `runs`, `show-run`, and `skills` commands
- `run-editor.py` for non-interactive atomic implementation
- agent dependency and validation targets
- architecture, setup, and upstream synchronization documentation
- focused runtime tests

The existing direct CLI remains intact as a fallback. No cloud or Claude dependency was
introduced.

Handoff hardening added:

- root `AGENTS.md` instructions for Codex
- explicit `HANDOFF.md` with verified state and known limitations
- virtualenv re-exec for direct CLI invocations
- all-or-nothing validation of generated exact-edit batches
- tracked plus untracked diff capture for review
- final status derived from verification and semantic-review verdict
- clean-tree handoff and smolagents smoke targets
