# Local Coder Architecture

This repository implements the architecture of the original local coding-stack research
without depending on Claude or cloud inference.

## Runtime flow

```text
Developer
   ↓
local-coder.py run
   ↓
smolagents CodeAgent orchestrator
   ├── explorer      → local-plan
   ├── planner       → local-plan
   ├── implementer   → local-fast → Aider
   ├── repairer      → local-fast → Aider
   └── reviewer      → local-review
        ↓
LiteLLM routing gateway :4000
        ↓
llama.cpp :8080
        ↓
Qwen2.5-Coder-3B Q4_K_M
```

Every run receives an isolated Git worktree. Agents can only use the narrow tools exposed
by `runtime/tools.py`; there is no unrestricted shell tool. Source edits are delegated to
Aider through the proven atomic-edit mode. Formatting, linting, tests, and protected
contract tests remain deterministic and authoritative.

## Skills

Role procedures live in `.local-coder/skills/*/SKILL.md`. Each skill selects its model
route, tool allowlist, and maximum steps. This keeps role prompts reusable and prevents a
large universal tool schema from consuming the small model's context.

## State and audit

`.local-coder/state/agent.db` records runs, agents, tool calls, artifacts, verification
results, and model metrics. The database and per-run task/review artifacts are ignored by
Git. Worktrees are preserved after a run so a human can inspect and merge or delete them.

## Hardware adaptation

The current GTX 1660 Ti / 8 GiB RAM setup uses one physical Qwen 3B model with three
logical LiteLLM aliases. The future `local-deep` profile is disabled and intended for a 7B
model loaded on demand rather than concurrently. Stable aliases allow the physical model
behind planning or review to change without changing the harness.

## Existing utilities

The earlier planner, plan executor, reviewer, and direct Aider CLI remain available as
fallback and debugging utilities. The agent runtime composes them rather than replacing
working components.
