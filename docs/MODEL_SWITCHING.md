# Qualified role activation and automatic model switching

**Status:** G4 evidence recorded; planner/reviewer activation implemented; live operator
validation required after applying the slice.

## Active role assignment

The committed activation manifest is
[`profiles/qwythos-role-activation-v1.json`](../profiles/qwythos-role-activation-v1.json).
It is accepted only when the canonical hashes of the committed baseline, candidate, and
final qualification reports remain valid and match the frozen G4 protocol. The manifest
also binds the canonical hash of `profiles/model-services-v1.json`, preventing an alternate
physical route policy from silently changing the promoted roles.

| Runtime role | Logical route | Physical profile |
| --- | --- | --- |
| Orchestrator | `local-plan` | Qwen `fast-qwen` |
| Explorer | `local-plan` | Qwen `fast-qwen` |
| Planner | `local-reason` | Qwythos `reason-qwythos` |
| Implementer | `local-fast` | Qwen `fast-qwen` |
| Repairer | `local-fast` | Qwen `fast-qwen` |
| Reviewer | `local-reason` | Qwythos `reason-qwythos` |

The planner uses the frozen `evidence-completeness` instructions and its qualified
1024-reasoning/1024-final generation profile. The reviewer uses `field-checklist` and its
qualified 1536-reasoning/1536-final profile. Generic active prompt states cannot override
these two qualification-bound prompts.

## Runtime behavior

The machine runs one llama.cpp server on port 8080. Before each model call, the trusted
runtime resolves the role's logical route and synchronously ensures that the matching
physical profile is live:

1. acquire an exclusive route lease and hold it through the complete inference request;
2. read `/health` and `/props`;
3. verify llama.cpp build, alias, model filename, context, slot count, and exact launch command;
4. stop the recognized current profile when a switch is required;
5. start the requested profile and wait for verified readiness;
6. record bounded switch and model-identity evidence; and
7. restore the prior recognized profile when the new profile fails to start.

An unknown, unidentified, or command-mismatched live llama.cpp process is not replaced or
stopped automatically. The runtime fails closed and requires operator inspection. Holding
the lease through inference also prevents a second local-coder process from switching the
physical model underneath an active request.

Switching is synchronous, not a background supervisor. A manager request switches back to
Qwen after a Qwythos specialist returns, so implementation and repair never inherit the
reasoning model merely because it was loaded for planning or review.

Port 8080 is an exclusive managed endpoint while this workflow is active. Do not send
direct concurrent inference requests to llama.cpp outside local-coder: those calls do not
participate in the route lease and therefore cannot be protected from a model switch.

## Operator commands

LiteLLM remains the only service that must be started separately:

```bash
litellm --config ~/code/local-coder/litellm-config.yaml \
  --host 127.0.0.1 \
  --port 4000
```

Normal commands select the required model automatically:

```bash
./local-coder.py run "Implement one bounded task"
./local-coder.py repair "Replace only the incorrect condition." calculator.py
./local-coder.py review TASK.md
```

Inspect or diagnose the manager explicitly:

```bash
./local-coder.py role-profiles
./local-coder.py model-service status
./local-coder.py model-service ensure local-plan
./local-coder.py model-service ensure local-reason
./local-coder.py model-service switch fast-qwen
./local-coder.py model-service switch reason-qwythos
./local-coder.py model-service stop
```

Environment overrides are available for machine-specific paths without editing committed
policy. Overrides must preserve the frozen binary and model filenames:

```text
LOCAL_CODER_LLAMA_SERVER
LOCAL_CODER_FAST_MODEL
LOCAL_CODER_REASON_MODEL
```

## Evidence and local state

Committed Track G evidence:

- `evidence/track-g/baseline-track-g-holdout-v1-20260724T031908Z.json`
- `evidence/track-g/candidate-track-g-holdout-v1-20260724T032051Z.json`
- `evidence/track-g/qwythos-holdout-qualification-v1.json`

Ignored runtime state is written under `.local-coder/model-services/`:

- `state.json` — current managed PID/profile record;
- `events.jsonl` — successful and failed switch events;
- `fast-qwen.log` and `reason-qwythos.log` — local server output; and
- `switch.lock` — cross-process serialization lock.

Model evidence records the resolved path, file size, modification time, and a SHA-256 value
when a sibling `<model>.sha256` file exists. The live service identity also records the
llama.cpp build, alias, context, slots, and model path.

## First live validation after applying

With LiteLLM running and no unrecognized process on port 8080:

```bash
cd ~/code/local-coder

./local-coder.py role-profiles
./local-coder.py model-service ensure local-plan
./local-coder.py model-service status
./local-coder.py model-service ensure local-reason
./local-coder.py model-service status
./local-coder.py model-service ensure local-plan

make verify
make agent-smoke
```

Then run one bounded agent task and inspect its model metrics. Planner and reviewer records
must name `local-reason`; orchestrator, explorer, implementation, and repair records must
remain on their Qwen routes. Switching failure must leave a clear error and, when a prior
recognized profile existed, restore that profile.

## Rollback

The bounded rollback is configuration-level: set `enabled` to `false` in
`profiles/qwythos-role-activation-v1.json`. The runtime then restores planner to
`local-plan` and reviewer to `local-review`, stops applying the qualification-bound prompt
instructions, and continues automatic Qwen service management. Set it back to `true` to
restore the recorded G4 activation.

Do not edit the frozen G4 reports, qualified role definitions, or fallback mappings. The
manifest remains hash-bound to the same evidence in either activation state.
