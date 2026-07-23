# ROADMAP: Reasoning-Capable Model Routes

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Planned — queued after Track E unless reasoning-route compatibility blocks
current work
**Track ledger:** Tracks A–D are complete and retired. Track E belongs to
[`MCP_CONTROL_PLANE.md`](MCP_CONTROL_PLANE.md). This roadmap claims **Track F**. The next
separate programme roadmap must start at Track G.

## 0. Why this document exists

`local-coder` currently assumes that a successful chat completion places a usable final
answer in `message.content`. Reasoning-capable models can instead return a separate
`message.reasoning_content` field while leaving `content` empty until the reasoning phase
finishes.

The live route probe exposed the concrete failure mode:

```text
finish_reason: length
content: ""
reasoning_content: "The user wants me to reply ..."
completion_tokens: 16
```

The route did not fail at transport. It exhausted the completion allowance during
reasoning and never produced the required final answer. Treating the reasoning text as
the answer would violate the planner, reviewer, JSON, and exact-output contracts.

This roadmap adds a general reasoning-model contract before introducing any specific
model. `Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf` is the first qualification target,
not a permanent architectural dependency. The existing 3B coding model remains the
default fast path unless replay evidence justifies an explicit role-level override.

Constraint mapping:

- **Stable local loop:** `local-fast`, `local-plan`, and `local-review` remain usable and
  unchanged while the optional `local-reason` route is qualified.
- **Bounded hardware use:** the first deployment uses one llama.cpp server and serial,
  operator-controlled model switching. It does not assume both models can remain resident.
- **Final-output integrity:** reasoning metadata is never substituted for missing final
  content, structured JSON, or tool calls.
- **No required cloud dependency:** all new behavior remains compatible with local GGUF,
  llama.cpp, and LiteLLM.
- **Audit without chain-of-thought retention:** persist reasoning presence, token counts,
  finish reason, and a hash when useful; do not store full reasoning traces by default.
- **Candidate isolation:** model qualification uses frozen development replay and an
  independent external holdout. Holdout evidence never becomes optimization input.

## 1. Track F — Reasoning-capable route integration

**Goal:** safely support an optional, on-demand reasoning model for planner and reviewer
work without weakening exact-output contracts, resource bounds, auditability, or the
existing fast coding path.

### F0. Freeze the reasoning response contract

Add one normalized response contract used by route probes and model adapters. It must
preserve these fields independently:

- final `content`;
- `reasoning_content`, whether returned directly or under provider-specific fields;
- parsed tool calls;
- `finish_reason`;
- prompt, completion, cached, and available reasoning-token accounting; and
- provider/model identity.

Required terminal classifications:

- `route_ok` — required final content is present;
- `tool_call_ok` — a valid required tool call is present;
- `reasoning_only_truncated` — reasoning exists, final content is empty, and generation
  stopped at the length limit;
- `empty_completion` — neither final content, reasoning, nor tool calls are usable;
- `malformed_final` — final content exists but violates the route contract; and
- `provider_error` — transport or provider failure.

Rules:

- Never copy `reasoning_content` into `content`.
- Never accept a reasoning-only response as a planner instruction, reviewer result,
  exact probe answer, GEPA proposal, or structured edit.
- Reject blank final instructions through the existing prompt-candidate guard.
- Store full reasoning only behind an explicit diagnostic option; the default record keeps
  bounded metadata and hashes.
- Add a regression fixture matching the observed empty-content response.

**Exit criteria:** every adapter and probe can distinguish a reasoning-only truncation from
an ordinary empty response, and existing non-reasoning routes retain identical behavior.

### F1. Make route probes reasoning-aware

Update `runtime/live_e2e.py` and its tests so probes exercise the contract they intend to
measure.

- The exact `ROUTE_OK` probe disables thinking per request, using a supported request
  control such as `chat_template_kwargs={"enable_thinking": false}`, and uses a small but
  sufficient final-answer allowance.
- A separate capability probe enables bounded reasoning and verifies that a final answer
  still appears after the reasoning phase.
- JSON and structured-output probes validate only final content or parsed tool calls.
- Probe diagnostics include route, finish reason, final-content presence, reasoning
  presence, and token usage without dumping full reasoning text.
- Stdout remains machine-readable; diagnostics remain on stderr or in explicit reports.
- The e2e report distinguishes configuration failure from budget starvation.

**Exit criteria:** the observed `content=""`, `reasoning_content!=null`,
`finish_reason="length"` response produces `reasoning_only_truncated` with an actionable
message rather than a generic empty-content error.

### F2. Add route-specific reasoning profiles and budgets

Extend route configuration without making one global generation policy serve every
model.

A reasoning-capable route profile must be able to declare:

- logical route name;
- llama.cpp/LiteLLM model alias;
- reasoning mode: `off`, `auto`, or `on`;
- bounded reasoning and final-completion allowances;
- temperature, top-p, top-k, and repetition penalty;
- timeout and retry policy;
- whether reasoning history preservation is required; and
- whether the route requires an operator-managed model switch.

Introduce `local-reason` as an additive optional route. Do not repoint `local-plan` or
`local-review` until qualification evidence exists. Token and call budgets must count all
provider-reported completion tokens, including tokens spent before the final answer.

Deliver profile examples for:

- exact probes with reasoning disabled;
- bounded planner reasoning;
- bounded reviewer reasoning; and
- long-form diagnostic evaluation with an explicit larger allowance.

**Exit criteria:** each route can carry its own reasoning and sampling policy, while the
three existing routes remain backward compatible and the default local loop starts with
no new service requirement.

### F3. Qualify Qwythos for planner and reviewer work

Treat Qwythos as a candidate route, not as a trusted upgrade by model reputation.

Qualification compares `local-reason` with the current planner/reviewer routes on frozen
replay suites and an independent holdout. Freeze acceptance thresholds before running the
comparison.

Measure at least:

- final-answer completion rate;
- exact schema and field adherence;
- empty-final and reasoning-only truncation rate;
- planner and reviewer case scores, including per-case regressions;
- malformed JSON and tool-call rate;
- prompt, reasoning/completion, and total token use;
- latency, generation throughput, startup time, and model-switch time;
- memory pressure and practical context limits on the current machine; and
- deterministic behavior of failure classification and retry limits.

Test both thinking-disabled exact tasks and thinking-enabled planning/review tasks. Start
from the model author's recommended sampling—temperature `0.6`, top-p `0.95`, top-k `20`,
and repetition penalty `1.05`—as a qualification input, then reduce output budgets to the
smallest values that pass the frozen suites. Do not adopt a universal 16K-token allowance
merely because the model card recommends a generous maximum.

Possible outcomes are independent:

- qualified for planner only;
- qualified for reviewer only;
- qualified for both;
- retained only as an operator-invoked diagnostic route; or
- rejected for local-coder use.

**Exit criteria:** no route becomes a default unless it satisfies frozen schema, quality,
regression, resource, and final-answer requirements. A failed qualification leaves every
existing route unchanged.

### F4. Add serial, on-demand model switching

The first supported lifecycle assumes one resource-constrained llama.cpp server.

- Add documented launch profiles for the fast coder and reasoning model.
- Add an operator script that stops the current local server, starts the selected profile,
  waits for health and model-identity checks, and fails closed on mismatch.
- Preserve the previous working profile so a failed switch can restore it.
- Record selected profile, model path hash where available, server version, route mapping,
  and readiness evidence.
- Refuse planner/reviewer invocation through `local-reason` when the expected model alias
  is not live.
- Do not assume simultaneous 3B and 9B residency.
- Keep automatic background supervision out of scope until manual serial switching proves
  reliable.

**Exit criteria:** an operator can switch between fast and reasoning profiles with one
bounded command, verify the active model, and return to the prior profile after failure.

### F5. Integrate qualified roles without changing write authority

After F3 and F4 pass:

- allow explicit planner and reviewer route selection through trusted configuration;
- keep explorer, implementer, and repairer on their existing routes by default;
- keep `runtime/editor.py` as the only agent source-writing boundary;
- record route and model identity in run, evaluation, and review evidence;
- prevent task text, candidate state, or model output from selecting its own stronger
  route;
- retain exact typed DSPy schemas and read-only reviewer boundaries; and
- permit GEPA reflection to use `local-reason` only through a separate explicit operator
  option after the route is qualified.

No existing alias is silently redirected. A later decision may map `local-plan` or
`local-review` to the qualified model, but that change requires its own frozen replay
comparison and rollback evidence.

**Exit criteria:** selected planner/reviewer calls can use the qualified route, all role
boundaries remain intact, and disabling the optional route restores the current behavior.

### F6. Benchmark MTP as an optional performance feature

The MTP GGUF does not enable speculative decoding by filename alone. Benchmark
`draft-mtp` explicitly against the same model with speculative decoding disabled.

Compare at least:

- no speculation;
- `draft-mtp` with small draft lengths such as 2 and 3;
- representative planner and reviewer prompt lengths;
- practical context sizes used by local-coder; and
- identical sampling and response contracts.

Record throughput, draft acceptance, latency, memory use, final-output validity, and any
context-size sensitivity. Do not combine speculative methods until each one is proven in
isolation. Remove unsupported flags, such as cache reuse for contexts where llama.cpp
reports it disabled.

MTP remains disabled by default unless it shows a repeatable net benefit on this exact
hardware without increasing failures or memory pressure.

**Exit criteria:** the chosen server profile is evidence-based. MTP may be enabled,
rejected, or retained as an experimental option without blocking the reasoning route.

## 2. Deliverables

Expected deliverables include:

- `docs/REASONING_MODELS.md` for operator behavior and failure meanings;
- a reasoning-response normalizer in the runtime model boundary;
- route-specific configuration for optional `local-reason`;
- reasoning-aware live e2e probes and reports;
- a Qwythos qualification manifest and reproducible report;
- fast and reasoning llama.cpp launch profiles;
- one bounded model-profile switch script;
- optional MTP benchmark scripts and captured evidence; and
- focused tests for normalization, truncation, budgets, role selection, switching, and
  rollback.

Exact module names may be refined during F0, but the response contract and trust
boundaries are fixed by this roadmap.

## 3. Phased delivery plan

- **Phase 0 — Response contract (F0):** normalize reasoning and final content separately
  and retain the observed failure as a deterministic regression test.
- **Phase 1 — Probe and route policy (F1–F2):** pass exact probes, reasoning probes, and
  route-specific budget tests without changing current routes.
- **Phase 2 — Model qualification (F3):** decide planner and reviewer suitability from
  frozen replay, independent holdout, and resource evidence.
- **Phase 3 — On-demand lifecycle (F4):** switch fast and reasoning profiles serially
  with identity checks, readiness checks, and recovery.
- **Phase 4 — Role integration (F5):** allow only explicitly configured, qualified roles
  to use `local-reason` while preserving write and audit boundaries.
- **Phase 5 — Optional acceleration (F6):** enable MTP only when its measured benefit is
  repeatable and contract-safe.

## 4. Explicit non-goals

- No immediate replacement of the current 3B coding model.
- No reasoning model as the default implementer or repairer.
- No use of reasoning text as a final answer, prompt candidate, structured edit, or tool
  call.
- No full chain-of-thought persistence by default.
- No unbounded generation allowance or universal reasoning budget.
- No assumption that both models remain loaded simultaneously.
- No automatic model download, cloud provider, or external inference requirement.
- No candidate-controlled route selection or budget expansion.
- No change to the nine internal agent tools or native editor authority.
- No requirement to enable MTP for reasoning-model support.
- No optimization against external holdout material.

## 5. Open questions for the primary actor

1. Should `local-reason` remain an explicit fourth route permanently, or may qualified
   planner/reviewer aliases later point to it while preserving their logical names?
2. Should the first model-switch command manage only llama.cpp, or also verify and reload
   LiteLLM routing in the same bounded operation?
3. Is hashed reasoning metadata sufficient for normal audit, with full traces available
   only through an explicit local diagnostic mode?
4. What latency and memory ceilings should be frozen for planner and reviewer
   qualification on the current machine?
5. Should GEPA reflection qualification be part of F5 or a later, separate roadmap item
   after planner/reviewer use is stable?

## 6. Completion and succession

When Track F is complete:

- mark this document complete rather than turning it into a permanent backlog;
- record qualification and operational evidence in `docs/HANDOFF.md` and
  `docs/VALIDATION_HISTORY.md`;
- update root `ROADMAP.md` to remove Track F from active work;
- keep rejected models and configurations as evidence, not active defaults; and
- allocate **Track G** to the next separate programme roadmap.

## 7. Technical references

- [llama.cpp server reasoning support][llama-server]
- [llama.cpp speculative decoding][llama-spec]
- [LiteLLM response and reasoning fields][litellm]
- [Qwythos model card][qwythos]

[llama-server]: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
[llama-spec]: https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md
[litellm]: https://docs.litellm.ai/
[qwythos]: https://huggingface.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M
