# llama.cpp Profiles

`llama-fast.ini` documents the Qwen 3B worker. `llama-deep.ini` remains a historical
on-demand example. The active serial launch policy is now
`model-services-v1.json`, which binds trusted Qwen and Qwythos commands to stable LiteLLM
routes. Keep only one model resident at a time on this hardware.

`qwythos-f3-qualification-v1.json` is not a launch profile. It is the retained first
F3 policy and historical decision contract. Its first focused collector combined structural
schema checks with fixture-specific expected answers, so it must not be used to compare
schema reliability between models without the correction below.

`qwythos-f3-focused-contract-v2.json` freezes the corrected diagnostic comparison protocol.
It runs the existing Qwen planner/reviewer profiles and the Qwythos reasoning profiles on
identical fixtures while keeping JSON validity, schema adherence, and task semantics as
separate measurements. It is diagnostic evidence, not a final qualification policy.

`qwythos-f3-adapter-contract-v1.json` freezes the operational comparison through the
shared `PlannerProgram` and `ReviewerProgram` entry points with `dspy.JSONAdapter`. Both
models receive identical fixtures; only their logical routes and bound runtime profiles
differ. It is focused diagnostic evidence and does not issue a qualification decision.

`track-g-development-v1.json` freezes the realistic development-set collection contract.
It binds the eight-case suite hash, one attempt per case, the role-oracle scorer, both
model identities, and their exact planner/reviewer route profiles. G2 uses only the
`baseline` subject; the candidate entry exists so G3 can reuse identical cases and scoring.

`track-g-qwythos-tuning-v1.json` freezes the G3 development-only Qwythos profile
experiment. It runs three role profiles twice per visible case, ranks accuracy before cost,
and permits holdout access only after bounded per-role gains without material regressions.
It does not change active runtime routes.

`track-g-qwythos-prompt-tuning-v1.json` freezes the G3.1 development-only prompt-contract
experiment. It holds the selected role generation settings constant, compares the
code-defined instructions with two reusable role-level candidates, and cannot access the
sealed holdout or activate a prompt.


`track-g-holdout-qualification-v1.json` freezes the G4 one-shot independent
comparison. It binds the sealed holdout and G3.1 selection hashes, exact Qwen and selected
Qwythos role configurations, one attempt per sealed case, and the final accuracy-first
qualification thresholds. It does not activate routes.

`qwythos-role-activation-v1.json` is the separate trusted activation decision. It validates
the committed baseline, candidate, and final G4 reports, promotes only planner and reviewer
to `local-reason`, retains their prior Qwen routes as explicit fallbacks, and leaves
explorer, orchestrator, implementer, and repairer on Qwen. Its `enabled` flag provides a
configuration-only rollback without changing the frozen qualification evidence.

`model-services-v1.json` is the active serial llama.cpp service policy. It maps Qwen to
`local-fast`, `local-plan`, and `local-review`, maps Qwythos to `local-reason`, and freezes
the exact launch arguments used by automatic synchronous switching. Its canonical hash
is bound by the role-activation manifest, so an alternate service policy fails closed.
