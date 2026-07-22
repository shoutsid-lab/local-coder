---
name: test-and-repair
description: Diagnose one deterministic verification failure and issue one atomic repair. Use only after verification fails and a narrowly bounded correction is required.
---
# Test and Repair

Use the provided tools for every diagnosis or repair, and return a final answer only
after using their evidence.

Treat verification output and protected tests as authoritative. Diagnose one failure at a
time. Translate it into one literal or tightly bounded instruction for the validated
native editor. Verify again before continuing. If a repair broadens scope, weakens a
contract, or makes the diff worse, roll the worktree back rather than compounding the
error.
