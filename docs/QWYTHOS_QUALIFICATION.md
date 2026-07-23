# Qwythos F3 qualification

**Status:** The first Qwythos focused/resource run completed. Its v1 contract field
combined schema adherence with fixture-specific task expectations, so it is retained as
historical resource evidence rather than used to decide whether Qwythos or Qwen has better
structured-output reliability. A corrected v2 baseline/candidate diagnostic protocol is
implemented. Track G quality cases, startup timing, model-switch timing, and the final
qualification policy remain pending.

This document describes the bounded F3 evidence surface for
`Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf`. It does not claim that the model is
qualified. Existing planner and reviewer route assignments remain unchanged.

## What the first run established

The first live collection was bound to implementation commit `003b831` and successfully:

- completed five exact, five planner, and five reviewer attempts;
- completed an additional exact request with 8,194 provider-reported prompt tokens;
- produced a final answer for every request;
- observed reasoning on every reasoning-enabled planner/reviewer request;
- recorded no malformed tool call;
- measured roughly 13.7 to 15.5 generated tokens per second;
- measured 5,912 MiB operator-observed VRAM under WSL; and
- measured approximately 3,493 MiB peak `llama-server` process RSS.

The report remains under the ignored `.local-coder/qualifications/` directory. It is valid
resource and response-completion evidence for that exact runtime configuration.

It is not a fair Qwen-versus-Qwythos schema comparison. The v1 collector's
`schema_valid` implementation checked both structure and fixture-specific expected
answers. In particular:

- the planner check required `editable_files == ["calculator.py"]` and an empty
  `depends_on` array; and
- the reviewer check required `verdict == "fail"`, at least one issue, and no unrelated
  changes.

A structurally valid JSON response with a different task conclusion was therefore recorded
as a schema failure. The resulting two-of-five planner and two-of-five reviewer rates
cannot distinguish malformed JSON, wrong fields, and semantic disagreement.

## Retained v1 policy and collector

[`../profiles/qwythos-f3-qualification-v1.json`](../profiles/qwythos-f3-qualification-v1.json)
is retained unchanged as the first frozen policy. Its hash-bound evidence and decision
engine remain reproducible. Do not edit it or reinterpret its combined `schema_valid`
field after collection.

`runtime/route_qualification_collect.py` remains the resource-oriented Qwythos collector.
It verifies the exact GGUF, `local-reason` alias, clean implementation commit, 8K context,
process RSS, and VRAM evidence. It persists no prompt, final-answer, or reasoning text.

Historical v1 commands remain available:

```bash
make route-qualification-policy-hash
make route-qualification-check
make route-qualification-collect-check
make route-qualification EVIDENCE=/path/to/qwythos-f3-evidence.json
```

The v1 decision engine should only receive a complete v1 evidence object whose role cases,
lifecycle measurements, and policy binding satisfy that historical schema. The first
focused report alone is not such an object.

## Corrected focused-contract protocol

[`../profiles/qwythos-f3-focused-contract-v2.json`](../profiles/qwythos-f3-focused-contract-v2.json)
freezes the corrected comparison protocol. It is deliberately separate from the final
qualification policy.

The protocol runs identical exact, planner, and reviewer fixtures against two operational
subjects:

- **baseline:** the existing Qwen model exposed by llama.cpp as `local-coder`, with
  `local-plan` and `local-review` using their current reasoning-disabled deterministic
  profiles; and
- **candidate:** Qwythos exposed as `local-reason`, with the proposed reasoning-enabled
  planner and reviewer profiles.

This is an operational route comparison, not a controlled scientific comparison of model
weights. The routes intentionally use the profiles that local-coder would actually deploy.
The fixture content, number of attempts, collector implementation, and machine identifier
are held constant.

Each attempt records these dimensions separately:

- `json_valid`: whether a planner/reviewer final answer is one raw JSON value;
- `schema_valid`: whether required fields and field types match the typed role contract;
- `task_semantics_valid`: whether the response reaches the expected conclusion for the
  synthetic boundary fixture;
- `contract_failures`: bounded classification codes such as `non_json_final`,
  `schema_mismatch`, or `task_semantics_mismatch`;
- normalized response outcome, final-answer presence, reasoning presence, and malformed
  tool-call status; and
- prompt/completion token counts, available reasoning-token count, latency, and generation
  throughput.

For the exact suite, JSON validity is not applicable. Exact string adherence is represented
by schema and task-semantic validity.

No prompt, final answer, or reasoning trace is written to the report. Reports are hash
bound to the protocol and created under `.local-coder/qualifications/` without replacing
prior evidence.

## Collect the baseline

Commit the diagnostic implementation first; collection refuses a dirty working tree.
Then run the existing Qwen server with the exact model and alias frozen in the protocol:

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/qwen2.5-coder-3b-instruct-q4_k_m.gguf \
  --alias local-coder \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --n-gpu-layers all \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --ubatch-size 256 \
  --metrics \
  --slot-save-path ~/llama.cpp/cache/
```

With LiteLLM running on port 4000:

```bash
make route-contract-diagnostic-check
make route-contract-diagnostic-collect \
  SUBJECT=baseline \
  ENVIRONMENT=amelia-gtx1660-v1
```

The default output is:

```text
.local-coder/qualifications/baseline-f3-contract-v2-<timestamp>.json
```

## Collect the candidate

Stop the Qwen server and start Qwythos as `local-reason` using the intended candidate
configuration. Only one model should be resident on this hardware.

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf \
  --alias local-reason \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --n-gpu-layers all \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --ubatch-size 256 \
  --reasoning-format deepseek \
  --reasoning-budget 1024 \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --metrics \
  --slot-save-path ~/llama.cpp/cache/
```

Collect the candidate under the same implementation commit and environment identifier:

```bash
make route-contract-diagnostic-collect \
  SUBJECT=candidate \
  ENVIRONMENT=amelia-gtx1660-v1
```

The default output is:

```text
.local-coder/qualifications/candidate-f3-contract-v2-<timestamp>.json
```

## Compare the reports

```bash
make route-contract-diagnostic-compare \
  BASELINE=.local-coder/qualifications/baseline-f3-contract-v2-<timestamp>.json \
  CANDIDATE=.local-coder/qualifications/candidate-f3-contract-v2-<timestamp>.json
```

An optional `OUTPUT=path.json` stores the comparison. The comparison refuses reports with
different protocol hashes, fixture versions, environment identifiers, or implementation
commits.

The result reports baseline metrics, candidate metrics, and candidate-minus-baseline rates
for each suite. Its `qualification_claim` is always `null`. Synthetic focused fixtures can
diagnose contract behavior but cannot establish real planner or reviewer quality.

## Remaining qualification work

After the corrected focused comparison:

1. inspect the bounded failure classes rather than raw model text;
2. decide whether prompt/profile changes are justified before freezing a second
   qualification policy;
3. run Track G development and independent holdout cases for both planner and reviewer;
4. measure cold startup and serial model-switch time; and
5. bind focused, resource, lifecycle, and real-task evidence into one new versioned
   decision contract.

The final policy must separate structural contract gates from scored task quality. It must
also use observed hardware behavior rather than treating an arbitrary VRAM reserve as a
model-quality failure.

Possible final outcomes remain independent:

- qualified for planner only;
- qualified for reviewer only;
- qualified for both;
- retained only as an operator-invoked diagnostic route; or
- rejected for local-coder use.

No evidence outcome changes runtime route assignments. Route activation remains later
Track F work after qualification and bounded switching are complete.
