# Qwythos development profile tuning

## Scope

Track G G3 tunes Qwythos only on the eight candidate-visible development cases. The
four-case holdout remains sealed. Both planner and reviewer continue to use the production
DSPy programs and `JSONAdapter`; only the bounded `local-reason` generation profile changes.

Accuracy is the primary objective. Latency is used only after score, stable case success,
minimum case score, and score variance are tied.

## Frozen profiles

[`../profiles/track-g-qwythos-tuning-v1.json`](../profiles/track-g-qwythos-tuning-v1.json)
contains three profiles. Every profile runs each development case twice.

| Profile | Planner | Reviewer | Purpose |
| --- | --- | --- | --- |
| `current-control` | 1024 reasoning + 1024 final, temperature 0.6 | same | Repeat the currently tested route profile and measure variance. |
| `deterministic-accuracy` | 1536 reasoning + 1536 final, temperature 0.0 | same | Test whether sampling noise and output compression are the main bottlenecks. |
| `role-depth-accuracy` | 2048 reasoning + 2048 final, temperature 0.2 | same | Test greater reasoning depth for both roles without the high-variance control sampling. |

The profiles do not change `runtime/route_profiles.py` or any active route. They are inert
evaluation inputs until final qualification and later route activation.

## Frozen selection policy

A profile must retain 100% adapter and schema success and no case attempt may score below
0.6. Eligible profiles are ranked independently for planner and reviewer by:

1. mean score;
2. stable case-success rate across both attempts;
3. minimum case score;
4. lower score variance; and
5. lower latency only as the final tie-breaker.

A role may proceed to holdout only when its selected profile:

- improves mean development score by at least 0.02 over `current-control`;
- introduces no case regression greater than 0.2; and
- satisfies every structural hard gate.

A combined planner/reviewer profile also requires at least 0.85 overall mean score and 0.5
stable case-success rate. The comparison emits no qualification claim and cannot activate a
route.

## Server configuration

Run only Qwythos while collecting these reports. Use a server reasoning default large
enough for the deepest frozen request; each request still supplies its exact profile budget.

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf \
  --alias local-reason \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --threads 6 \
  --threads-batch 12 \
  --n-gpu-layers all \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --ubatch-size 256 \
  --reasoning-format deepseek \
  --reasoning-budget 2048 \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --metrics \
  --slot-save-path ~/llama.cpp/cache/
```

LiteLLM must continue exposing `local-reason` on port 4000.

## Collect

Commit the tuning implementation first. Collection requires one clean implementation
commit and unchanged active prompt lineage.

```bash
make real-task-profile-tuning-collect \
  PROFILE=current-control \
  ENVIRONMENT=amelia-gtx1660-v1

make real-task-profile-tuning-collect \
  PROFILE=deterministic-accuracy \
  ENVIRONMENT=amelia-gtx1660-v1

make real-task-profile-tuning-collect \
  PROFILE=role-depth-accuracy \
  ENVIRONMENT=amelia-gtx1660-v1
```

Reports are written under `.local-coder/real-task-evidence/`. They retain case identities,
bounded dimension results, failure codes, latency, and available token counts. They do not
retain generated fields, prompts, final answers, or reasoning text.

## Compare

```bash
CONTROL="$(ls -1t \
  .local-coder/real-task-evidence/current-control-track-g-tuning-v1-*.json | \
  head -n 1)"
DETERMINISTIC="$(ls -1t \
  .local-coder/real-task-evidence/deterministic-accuracy-track-g-tuning-v1-*.json | \
  head -n 1)"
DEEP="$(ls -1t \
  .local-coder/real-task-evidence/role-depth-accuracy-track-g-tuning-v1-*.json | \
  head -n 1)"

make real-task-profile-tuning-compare \
  REPORTS="$CONTROL $DETERMINISTIC $DEEP" \
  OUTPUT=.local-coder/real-task-evidence/qwythos-tuning-selection-v1.json
```

The comparator rejects missing profiles, duplicate profiles, different commits,
environments, service identities, prompt lineage, suite hashes, or altered report contents.
It selects planner and reviewer profiles independently and states which roles, if any, are
ready for the sealed holdout.

## Recorded G3 result

The three clean development reports completed at implementation commit `6f3e473`.
`current-control` retained the highest overall mean score (approximately 0.846) and highest
planner mean (approximately 0.792), but one planner attempt scored 0.5 and therefore failed
the frozen 0.6 minimum-case hard gate. Among eligible profiles, `role-depth-accuracy` won
the planner tie only on latency, while `deterministic-accuracy` won reviewer selection by
stable case success and won the eligible overall tie. No role gained the required 0.02 mean
score over control, so holdout remained sealed.

Increasing reasoning depth did not remove the repeated planner acceptance-criteria and
scope-reference failures. The next experiment therefore holds generation settings fixed and
tests reusable prompt-contract candidates described in
[Qwythos development prompt-contract tuning](QWYTHOS_PROMPT_TUNING.md).
