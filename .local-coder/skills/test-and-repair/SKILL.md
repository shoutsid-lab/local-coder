---
name: test-and-repair
description: Diagnose one deterministic verification failure and issue one atomic repair. Use only after verification fails and a narrowly bounded correction is required.
compatibility: Requires deterministic verification, exact editing, diff inspection, and rollback.
---
# Test and Repair

Use deterministic verification evidence for every diagnosis and the client's validated
exact-edit capability for every repair. Treat verification output and protected tests as
authoritative.

Diagnose one failure at a time. Translate it into one literal or tightly bounded edit,
then verify again before continuing. If a repair broadens scope, weakens a contract, or
makes the diff worse, roll back rather than compounding the error.

Use the [bounded repair loop](references/REPAIR_LOOP.md) when cross-testing with equivalent
client tools.
