# Qwythos F3 qualification

**Status:** Qualification policy and decision contract implemented; live contract,
resource, development-corpus, and final-holdout evidence still pending.

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
