# ROADMAP: MCP Control-Plane Integration

**Target repository:** `shoutsid-lab/local-coder`

**Status:** Active — detailed implementation roadmap, indexed by
[`ROADMAP.md`](../../ROADMAP.md)
**Track ledger:** Tracks A–D are complete and retired. Their labels must not be reused.
This roadmap claims **Track E**. The next detailed programme roadmap must start at
Track F. Root-roadmap identifiers such as `R1` are queue identifiers, not track labels.

## 0. Why this document exists

The root [`ROADMAP.md`](../../ROADMAP.md) remains the repository-wide active queue and
index. This document is the detailed implementation plan for one active programme:
exposing the trusted external control-plane CLI through Model Context Protocol (MCP).

The completed Agent Skills, DSPy, GEPA, paired-evaluation, and prompt-deployment work is
part of the stable baseline recorded in [`../HANDOFF.md`](../HANDOFF.md) and
[`../RECURSIVE_IMPROVEMENT.md`](../RECURSIVE_IMPROVEMENT.md). Nothing in Tracks A–D is
reopened here.

`local-coder` already has an interface separate from the internal agent runtime: a
**trusted external actor**—a human, trusted service, or more capable model—that can:

- produce a `task-plan.json`;
- validate and hash it through `validate-plan`;
- execute one approved step at a time through `run-plan-step --approve-plan-hash ...`;
- inspect read-only evidence through `runs`, `show-run`, `analyze-runs`,
  `audit-campaign`, and `status`; and
- drive the explicit campaign lifecycle without giving authority to the candidate under
  evaluation.

Today this surface is CLI-only, so an operator must relay JSON and shell output manually.
MCP standardizes the same interaction shape: an external client discovers and calls a
small set of typed tools exposed by a server. This roadmap exposes the **existing,
already-audited CLI surface** through an optional MCP server. It does not create a second
control plane.

This programme does not modify the internal agent runtime. The nine tools in
`runtime/tools.py`, the native editor's exclusive source-write authority, isolated Git
worktrees, and the LiteLLM/llama.cpp inference stack remain unchanged. **MCP is not added
to the `smolagents CodeAgent` tool list.**

Constraint mapping:

- **No unrestricted shell tool:** MCP tools map to named CLI subcommands with fixed
  argument construction. There is no raw command or shell passthrough.
- **Avoid a universal schema in the small model's context:** the MCP schema is served to
  an external trusted-planner client and is never loaded into the 3B runtime model's
  context.
- **No required cloud dependency for the local loop:** the MCP server is optional
  operator tooling. `./local-coder.py run`, `repair`, and `review` never depend on it.
- **Candidates cannot authorize promotion or gain write authority:** the MCP layer
  preserves every plan hash, approval, scorecard, and decision gate. It never generates
  missing approvals or bypasses the CLI.
- **Structured stdout must remain machine-readable:** the server captures child-process
  output and returns it inside MCP tool results. Under stdio, only valid MCP messages are
  written to server stdout; diagnostics go to stderr.

## 1. Track E — MCP server over the trusted-planner CLI

**Goal:** let an MCP-aware client act as the trusted external planner and auditor by
calling typed tools instead of requiring a human to relay CLI output.

### E0. Groundwork and dependency boundary

- Draft `docs/MCP_INTEGRATION.md` before registering write tools.
- Add `requirements-mcp.txt` as an operator-side dependency set. Do not merge it into
  `requirements-agent.txt`.
- Resolve whether the server belongs in `runtime/mcp_server.py` or a sibling
  `mcp_server/` package. The chosen location must not blur the internal agent-runtime
  boundary.
- Start with the standard **stdio** transport only.
- Keep the tool registry empty until process invocation, output capture, timeout, and
  error contracts have deterministic tests.
- Invoke `local-coder.py` with an argument vector and `shell=False`; never construct a
  shell command string from tool input.
- Capture stdout and stderr separately. Return command stdout as tool content, include
  bounded diagnostics where useful, and never allow child output to corrupt MCP stdio.
- Apply bounded subprocess timeouts and output limits consistent with the existing CLI
  and campaign budgets.

**Exit criteria:** the server initializes and shuts down cleanly through stdio, exposes no
capability beyond discovery, and leaves `make verify` and `make agent-smoke` unchanged.

### E1. Read-only tools first

Register these tools as direct adapters over existing CLI subcommands:

- `status` → `./local-coder.py status`
- `list_runs` → `./local-coder.py runs`
- `show_run(run_id)` → `./local-coder.py show-run RUN_ID`
- `analyze_runs(limit)` → `./local-coder.py analyze-runs --limit N`
- `audit_campaign(campaign_id)` → `./local-coder.py audit-campaign CAMPAIGN_ID`

Rules:

- Tool input schemas validate transport-level types and required fields.
- The existing CLI remains authoritative for domain validation and authorization.
- The adapter does not query SQLite, import private command handlers, or reimplement
  command semantics.
- CLI stdout is captured and returned as tool content; stderr and non-zero exit state are
  represented as bounded tool errors.
- `make mcp-server` starts the stdio server as a non-protected development target,
  separate from `make verify`.

**Exit criteria:** all five tools are callable from an MCP client, preserve the CLI's
read-only behavior, and return results equivalent to direct CLI invocation.

### E2. Gated write tools

Add one tool per existing subcommand. Do not bundle several state transitions into an
implicit action.

Plan tools:

- `validate_plan(task_plan_json)` → `validate-plan`, returning the approved plan hash.
- `run_plan_step(task_plan_json, step_id, approved_plan_hash)` → `run-plan-step
  --approve-plan-hash ...`.

Campaign tools:

- `create_campaign`
- `approve_brief`
- `build_candidate`
- `evaluate`
- `record_decision`
- `close_campaign`

Rules:

- Every tool requires the same explicit arguments as its CLI counterpart.
- `run_plan_step` rejects a missing approved hash at schema validation and still relies
  on the CLI for hash matching and execution policy.
- Approval, actor, rationale, evaluation, and decision fields are never synthesized by
  the server.
- The server relays trusted-actor input; it does not become the trusted actor.
- Tool calls preserve the existing SQLite audit trail and return the CLI's machine-readable
  result without adding a parallel record format.
- Side-effecting tool names and descriptions clearly identify their write behavior so MCP
  clients can apply their own confirmation policy.

**Exit criteria:** a complete trusted-plan run through MCP produces the same state and
audit evidence as direct CLI use, and every missing or invalid approval fails closed.
Campaign transitions show the same parity independently.

### E3. External read-only evidence adapter — optional and deferred

This is speculative and not committed by E0–E2.

A future explorer/planner adapter may consume external context from a local, vetted,
read-only MCP server, such as package documentation or an issue-tracker ticket. That
would be the first case where the internal runtime consumes another MCP server rather
than exposing its own trusted CLI.

Do not start E3 until:

- E1 and E2 have shipped and produced validation evidence;
- the maintainer explicitly approves the new dependency direction;
- the effect on the 3B model's context budget is measured;
- the source is local or otherwise explicitly configured; and
- evidence is normalized into the existing read-only explorer contract before the model
  sees it.

**Exit criteria:** no default-on cloud dependency, no write-capable external adapter, and
no expansion of the internal nine-tool surface.

## 2. Deliverables

- MCP server module at the package location resolved in E0.
- `docs/MCP_INTEGRATION.md`.
- `requirements-mcp.txt`.
- `make mcp-server`.
- `tests/test_mcp_server.py` as a non-protected test module.
- CLI-parity fixtures covering successful output, stderr diagnostics, non-zero exits,
  timeouts, malformed input, and approval failures.

## 3. Phased delivery plan

- **Phase 0 — Groundwork (E0):** document the optional dependency boundary; start an
  empty stdio server; keep core runtime verification unaffected.
- **Phase 1 — Read-only tools (E1):** match direct CLI results and prove that the MCP
  surface cannot mutate repository or campaign state.
- **Phase 2 — Gated write tools (E2):** preserve CLI-equivalent hashes, approvals,
  decisions, and audit lineage for plan and campaign operations.
- **Phase 3 — External evidence adapter (E3, optional):** start only after explicit
  maintainer approval and introduce no default-on external dependency.

## 4. Explicit non-goals

- No MCP client is added to the `smolagents CodeAgent` orchestrator or
  `runtime/tools.py`.
- No raw shell, arbitrary executable, generic subprocess, file-write, or database tool is
  exposed.
- No new source-write authority is introduced. `runtime/editor.py` remains the only
  component that writes source during agent runs.
- No relaxation or automatic supply of `--approve-plan-hash`, brief approvals,
  scorecard-derived decisions, or deployment eligibility.
- No required dependency is added to `./local-coder.py run`, `repair`, or `review`.
- No MCP client for third-party servers is added in E0–E2.
- No Streamable HTTP listener is added in the initial delivery.
- No protected architecture or evaluation contract is changed merely to fit the MCP
  library. Any genuinely required architectural change must be proposed separately.

## 5. Open questions for the primary actor

1. Should the server live under `runtime/`, or in a sibling `mcp_server/` package to keep
   the internal execution package narrowly scoped?
2. After stdio is proven, is Streamable HTTP useful for a remote trusted planner? If so,
   what localhost binding, authentication, Origin validation, and session policy is
   required before it can be enabled?
3. Should plan write tools and campaign-lifecycle tools ship together in E2, or should
   campaign tools become E2.2 because they touch the recursive-improvement trust
   boundary?
4. Should read-only tools return the CLI's JSON as text for strict parity, or also declare
   MCP output schemas while retaining the original text payload for compatibility?

## 6. Completion and succession

When Track E is complete:

- mark this document complete rather than replacing it with another active plan;
- record delivered evidence in `docs/HANDOFF.md` and `docs/VALIDATION_HISTORY.md`;
- update root `ROADMAP.md` to remove Track E from active work; and
- leave Track F to the already allocated
  [`REASONING_MODEL_ROUTES.md`](REASONING_MODEL_ROUTES.md) programme and allocate
  **Track G** to the next separate roadmap.
