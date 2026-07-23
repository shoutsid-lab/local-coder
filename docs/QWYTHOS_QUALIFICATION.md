# Qwythos F3 qualification

**Status:** Qualification policy, decision contract, and focused live collector
implemented; the first machine run, startup/switch timing, development corpus, and final
holdout evidence remain pending.

This document describes the bounded F3 decision surface for
`Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf`. It does not claim that the model is
qualified. Existing planner and reviewer route assignments remain unchanged until a
complete evidence report passes the frozen policy.

## Frozen policy

[`../profiles/qwythos-f3-qualification-v1.json`](../profiles/qwythos-f3-qualification-v1.json)
is the machine-readable source of truth. The report must bind to its canonical SHA-256,
the exact candidate model identity, `local-reason`, the existing planner/reviewer
baselines, and the tested role generation profiles.

The first policy freezes:

- five thinking-disabled exact attempts;
- five thinking-enabled planner contract attempts;
- five thinking-enabled reviewer contract attempts;
- complete final-answer and schema adherence in all focused contract suites;
- no reasoning leakage in exact mode and observable reasoning in reasoning mode;
- no non-`route_ok` outcome or malformed tool call;
- at least four development and two independent holdout cases per role;
- no aggregate score or success-rate regression;
- no loss of a baseline success, out-of-scope change, or material per-case regression;
- no more than 0.5 additional mean repair iterations;
- bounded startup, switch, memory, throughput, context, and p95 latency limits for the
  current local machine.

Changing a threshold requires a new policy version. Do not edit the active policy after
collecting evidence against its hash.

## Focused live collection

`runtime/route_qualification_collect.py` performs the evidence that can be gathered before
Track G cases exist. It refuses to run from a dirty tree and verifies all of the following
before sending a model request:

- `/health` reports ready;
- `/props` names the exact frozen GGUF basename;
- `/v1/models` exposes the `local-reason` alias;
- the configured context can accommodate the frozen 8K probe and final allowance;
- one unambiguous `llama-server` PID owns the named model; and
- process RSS can be sampled and VRAM evidence is available from process attribution or
  an explicit independent measurement.

The collector then runs five thinking-disabled exact attempts, five reasoning-enabled
planner attempts, five reasoning-enabled reviewer attempts, and one additional exact
request whose provider-reported prompt use is at least 8,192 tokens. Planner and reviewer
fixtures use their real typed field contracts without a response grammar, so schema
success remains model evidence rather than grammar enforcement. Sampling and generation
budgets come directly from the frozen policy.

No prompt response, final answer, or reasoning trace is written to disk. Each attempt
stores only the normalized outcome, final/schema/reasoning presence, malformed-tool-call
status, token counts, latency, and throughput. Reports are created atomically under the
ignored `.local-coder/qualifications/` directory and are never overwritten.

Prerequisites:

```bash
# Qwythos must already be the only llama.cpp model on :8080.
# Required server property:
#   --alias local-reason
# --metrics is strongly preferred for direct generation throughput.
# LiteLLM must already be routing local-reason through :4000.
make route-qualification-collect-check
make route-qualification-collect ENVIRONMENT=amelia-gtx1660-v1
```

If process discovery is ambiguous, pass `SERVER_PID=<pid>`. NVIDIA documents
`nvidia-smi` as having a limited feature set under WSL 2, so a driver that cannot attribute
memory to the Linux server PID may instead use an independently observed
`PEAK_VRAM_MIB=<value>`. Separately measured lifecycle values can also be bound during
collection:

```bash
make route-qualification-collect \
  ENVIRONMENT=amelia-gtx1660-v1 \
  STARTUP_SECONDS=52.4 \
  MODEL_SWITCH_SECONDS=71.8 \
  PEAK_VRAM_MIB=5210
```

Without those values, the report marks them as pending. F4 owns reproducible serial
startup and switch measurement. The focused report is deliberately not accepted as a
final qualification report: Track G must still provide corpus identity and planner/reviewer
baseline-versus-candidate cases before `make route-qualification` can decide a role.

Resource meanings are fixed for this collector:

- peak system memory is the sampled `llama-server` `VmRSS` from `/proc`;
- peak VRAM is NVIDIA memory attributed to that server PID, or an explicit independent
  measurement when WSL does not expose process attribution;
- tested context is the prompt-token count returned by the live context request; and
- generation throughput prefers llama.cpp's `predicted_tokens_seconds` metric, falling
  back to completion tokens divided by total request latency when metrics are unavailable.

## Evidence contract

The input report is one JSON object with:

- policy, candidate, route, role-profile, baseline-route, implementation-commit, corpus,
  and environment identities;
- individual focused contract attempts for `exact`, `planner`, and `reviewer`;
- individual planner and reviewer cases split into `development` and `holdout`;
- baseline and candidate scores, success flags, repair iterations, and scope results;
- normalized response outcome, final/schema validity, malformed-tool-call status, and
  bounded reasoning presence;
- prompt, completion, and available reasoning-token counts;
- latency and generation throughput per attempt or case; and
- startup time, model-switch time, peak VRAM, peak system memory, and tested context.

Full reasoning text is neither required nor accepted. Track G owns corpus construction,
case provenance, deterministic verification, and holdout isolation. F3 only validates the
complete report and derives a decision from the frozen evidence.

## Commands

Print the policy hash before collecting evidence:

```bash
make route-qualification-policy-hash
```

Run the deterministic contract tests:

```bash
make route-qualification-check
```

Evaluate a completed report:

```bash
make route-qualification \
  EVIDENCE=/path/to/qwythos-f3-evidence.json
```

Require a specific outcome when scripting the operator workflow:

```bash
make route-qualification \
  EVIDENCE=/path/to/qwythos-f3-evidence.json \
  REQUIRE=both
```

`REQUIRE` may be `any`, `planner`, `reviewer`, or `both`. A valid report always prints the
full machine-readable decision. An unmet required outcome exits with status 2; malformed,
incomplete, stale-policy, or identity-mismatched evidence exits with status 1.

## Decision outcomes

The decision engine can return:

- `qualified_for_both`;
- `qualified_for_planner_only`;
- `qualified_for_reviewer_only`;
- `diagnostic_only` when contracts and resources pass but both role comparisons fail; or
- `rejected` when a global contract or resource gate fails.

Planner and reviewer quality gates are evaluated independently. Global response-contract
or resource failures reject both roles. No outcome changes runtime route assignments;
that remains F5 work after successful F3 evidence and bounded F4 model switching.
