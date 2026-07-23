# llama.cpp Profiles

`llama-fast.ini` describes the current Qwen 3B worker tuned for the GTX 1660 Ti.
`llama-deep.ini` is a disabled on-demand profile for a future 7B planner/reviewer.

The INI files are documentation and launch inputs for a future profile manager; they do
not start servers by themselves. Keep only one large model resident at a time on this
hardware. LiteLLM aliases (`local-fast`, `local-plan`, `local-review`) remain stable even
when their physical backend changes.

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
