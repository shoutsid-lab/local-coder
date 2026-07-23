# Validation History

This file retains the evidence that still informs the current architecture without
keeping the full chronological narrative in the completed [`HANDOFF.md`](HANDOFF.md).

## Baseline

The agent-runtime work began from GitHub `shoutsid-lab/local-coder` commit
`8f12ea1f78fd692017797591cf1ee1948b8d7b1d`. That baseline remains part of Git history; no separate baseline manifest is maintained.

## Controls established by live trajectories

| Run | Observation | Control retained |
| --- | --- | --- |
| `8b8c4f60fdad` | First bounded README edit completed with deterministic verification and review. | Worktree isolation and explicit approval remain the normal flow. |
| `d3720aea52f0` | The replacement native editor applied one exact edit and preserved scope. | Strict schema, approved paths, and exact-match validation remain mandatory. |
| `43bc88984ee8` | The fixed read-only reviewer completed a clean committed-source regression. | Reviewer has no code executor or write operation. |
| `9bf491ef79c7` | Redundant delegation caused seven rejected edits and exposed an ignored `.venv` symlink. | Rejected edits force attention; expected `.venv` is ignored and other symlinks are rendered. |
| `9affc7dd61b0` | A failed final review left stale successful state visible. | Every review clears prior artifact and verdict state before invocation. |
| `031fb5dac244` | Review failure triggered repeated calls and incorrectly erased passing verification. | Review unavailability is bounded and cannot overwrite deterministic evidence. |
| `b3d35207a6b1` | One rejected edit and two bounded malformed-review attempts ended safely. | Final status was `needs_attention`, verification stayed true, and the verdict stayed null. |

Before the documentation and legacy-example cleanup on 2026-07-22, the deterministic
suite contained 43 passing tests. The active suite now focuses on runtime and architecture
contracts; current counts belong in command output rather than this historical record.

The recursive-improvement completion pass added schema-v8 campaign identity binding,
pre-execution persistence of candidate patch and trajectory lineage, and a read-only
campaign audit. Deterministic tests cover migration from schema v7, holdout and
environment mismatch rejection, clean campaign closure, artifact tamper detection, and
terminal evaluation lineage retention.

The authorization-language pass removed person-specific assumptions from code, CLI output,
tests, and documentation. Promotion scorecards now end at the efficiency gate and emit
`eligible_for_promotion`; brief approval and promotion decisions accept any nonempty
actor identity with a rationale. Legacy scorecards with the former trailing authority gate
remain promotion-compatible through structural gate validation.

The active planning entry point is now [`../ROADMAP.md`](../ROADMAP.md), while
[`HANDOFF.md`](HANDOFF.md) remains the completed recursive-improvement baseline. This
separates current work from historical delivery evidence without changing the trust model.

## Prompt optimization and deployment evidence

The completed DSPy/GEPA programme added typed role programs, audited dataset export,
bounded prompt optimization, explicit candidate outcomes, paired external holdout
evaluation, and promotion-bound prompt deployment. The live planner campaign produced a
changed candidate that improved aggregate development and holdout scores but regressed
three individual external holdout cases. The ordered regression gate correctly rejected
the candidate.

The unattended prompt lifecycle then derived the rejection from the frozen scorecard,
closed and audited the campaign, and confirmed that no active prompt state was written.
The final Track D verification reported 231 passing tests in the project environment,
including focused prompt-campaign, evaluation, activation, and rollback coverage.

## Proven boundary

The evidence proves bounded exact edits, complete diff inspection, deterministic
verification, conservative status derivation, frozen campaign identities, auditable
bounded recursion, and preserved external authorization. It does not prove broad
autonomous decomposition or safe candidate self-promotion.

## Strategic evidence reset

The prompt campaign and deployment work established prevention and audit guarantees, but
not a positive capability result: the only live optimized planner candidate was rejected,
and the original seed corpus was synthetic. The repository therefore froze additional
GEPA and deployment hardening behind an explicit evidence gate.

Track F now tests an optional reasoning-capable planner/reviewer route, while Track G
builds a benchmark from real repository tasks and failures. Future roadmap investment is
to follow measured task outcomes rather than control-plane completeness. Existing
candidate-neutral authorization remains as integrity protection and future-capability
insurance, not as a claim that the current 3B model is an active adversary.

## Reasoning response contract

Track F0 added one provider-neutral response normalizer for live probes, structured output,
the native editor, and smolagents audit wrapping. The observed `content=""`,
`reasoning_content!=null`, `finish_reason="length"` response is now classified as
`reasoning_only_truncated` with an actionable diagnostic. Full reasoning traces remain
outside normal audit state; presence, size, token counts, identity, and SHA-256 metadata
are retained when relevant. Ordinary non-reasoning response metadata remains unchanged.

## Reasoning-aware probe policy

Track F1 separated exact route health checks from reasoning-capability checks. Exact probes
now disable thinking through LiteLLM passthrough controls and use a bounded 64-token final
allowance. The optional reasoning probe enables a 128-token thinking budget within a
256-token completion ceiling and requires both observable reasoning metadata and the exact
final answer. Focused tests preserve the observed reasoning-only truncation failure, detect
ignored exact-probe controls, and confirm that full reasoning text is never emitted in the
probe report.
