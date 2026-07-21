# Validation History

This file retains the evidence that still informs the current architecture without
keeping the former chronological narrative in `HANDOFF.md`.

## Baseline

The agent-runtime work began from GitHub `shoutsid-lab/local-coder` commit
`8f12ea1f78fd692017797591cf1ee1948b8d7b1d`. Exact historical blob IDs are recorded in
[`UPSTREAM.json`](UPSTREAM.json).

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

## Proven boundary

The evidence proves bounded exact edits, complete diff inspection, deterministic
verification, conservative status derivation, and preserved human authority. It does not
prove broad autonomous decomposition or safe recursive self-promotion.
