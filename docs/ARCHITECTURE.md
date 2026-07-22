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
   ├── explorer      → read-only evidence adapter → local-plan
   ├── planner       → read-only evidence adapter → local-plan
   ├── implementer   → code-action leaf → local-fast → validated exact edits
   ├── repairer      → code-action leaf → local-fast → validated exact edits
   └── reviewer      → fixed read-only review adapter → local-review
        ↓
LiteLLM routing gateway :4000
        ↓
llama.cpp :8080
        ↓
Qwen2.5-Coder-3B Q4_K_M
```

The separate trusted improvement path is:

```text
normalized audit evidence → one approved brief → committed candidate
        ↓
networkless read-only baseline/candidate sandboxes
        ↓
candidate verification + base-owned development/holdout contracts
        ↓
lexicographic scorecard → authorized decision record
```

Every run receives an isolated Git worktree. Agents can only use the narrow tools exposed
by `runtime/tools.py`; there is no unrestricted shell tool. Source edits pass through
`runtime/editor.py`, which requests strict JSON search/replace operations, validates
approved paths and unique exact matches in memory, and writes nothing unless the complete
batch is valid. Formatting, linting, tests, and protected contract tests remain
deterministic and authoritative.

## Skills

Role procedures live in `.local-coder/skills/*/SKILL.md`. Each skill selects its model
route, tool allowlist, and maximum steps. This keeps role prompts reusable and prevents a
large universal tool schema from consuming the small model's context.

## State and audit

`.local-coder/state/agent.db` records runs, agents, tool calls, artifacts, verification
results, and model metrics. The database and per-run task/review artifacts are ignored by
Git. Worktrees are preserved after a run so an authorized actor can inspect and
merge or delete them.

## Hardware adaptation

The current GTX 1660 Ti / 8 GiB RAM setup uses one physical Qwen 3B model with three
logical LiteLLM aliases. The future `local-deep` profile is disabled and intended for a 7B
model loaded on demand rather than concurrently. Stable aliases allow the physical model
behind planning or review to change without changing the harness.

## Trusted improvement boundary

Recursive improvement is generational rather than online self-modification. The trusted
evaluator runs outside candidate worktrees, compares immutable baseline and candidate
generations, and produces a recommendation only. Candidates must not control
their evaluator, contracts, holdout cases, or promotion policy. Promotion authority may
be delegated to a trusted external actor, including a more capable model, but never to the
candidate being evaluated.

`evaluation/supervisor.py` uses bubblewrap to expose only candidate runtime inputs to
base-owned contract workers. Candidate-owned verification receives a read-only checkout,
an ephemeral size-bounded `/tmp`, no network, and the trusted Python environment. The
evaluator never supplies an unrestricted shell command. Sandboxed commands run under an
unprivileged UID with capabilities dropped, while the base-owned process guard installs
a kernel process-count ceiling before candidate code executes.

Production holdout manifests and oracles live only in ignored
`.local-coder/holdout/<rotation>/` storage after validation and immutable provisioning
from an external operator-controlled source. Campaign commands reject holdout paths from
candidate-visible Git content. A campaign freezes the holdout manifest-plus-oracle hash
and evaluator environment hash at creation, and evaluation rejects either identity if it
changes.

Completed campaign lineage can be checked with the read-only `audit-campaign` command.
The audit verifies bounded builds, paired evidence, archived artifact hashes, scorecard
ordering, and authorization decisions without invoking Git or modifying SQLite.

The direct reviewer and native repair CLI remain available as focused debugging
utilities. They use the same read-only review and native editor boundaries as the agent
runtime.
