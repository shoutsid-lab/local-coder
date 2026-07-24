# Qwythos F3 qualification

**Status:** The first Qwythos focused/resource run completed. Its v1 contract field
combined schema adherence with fixture-specific task expectations, so it is retained as
historical resource evidence. The corrected raw-route v2 diagnostic has now been collected
for both models, but it bypasses the DSPy role adapters and therefore measures native
output behavior rather than operational planner/reviewer reliability. The shared-adapter
comparison passed for both models. Track G now has a frozen realistic development/holdout
corpus and a per-case development runner; the Qwen baseline, Qwythos
comparison, startup timing, model-switch timing, and final qualification policy remain
pending.

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

## Shared-adapter comparison

The raw v2 runs established native route behavior only. Qwythos returned bare JSON more
often than Qwen, while Qwen was substantially faster. That result is not an operational
planner/reviewer comparison because the current local-coder routes rely on DSPy's typed
role programs and `JSONAdapter` to produce their final contracts.

[`../profiles/qwythos-f3-adapter-contract-v1.json`](../profiles/qwythos-f3-adapter-contract-v1.json)
freezes the next comparison. Both subjects receive the same fixtures and invoke the same
entry points:

```text
run_planner_program -> PlannerProgram -> dspy.JSONAdapter
run_reviewer_program -> ReviewerProgram -> dspy.JSONAdapter
```

Only the logical model route changes. The baseline uses `local-plan` and `local-review`;
the candidate uses `local-reason` for both roles. Each report binds the active model,
llama.cpp alias, implementation commit, environment identifier, and exact runtime route
profiles. It stores only bounded adapter-success, schema, semantic, token, and latency
classifications. No fixture prompt, generated field text, final answer, or reasoning text
is retained.

After committing the tooling, collect whichever model is currently resident:

```bash
make route-adapter-diagnostic-collect \
  SUBJECT=candidate \
  ENVIRONMENT=amelia-gtx1660-v1
```

Switch models and collect the other subject under the same clean implementation commit:

```bash
make route-adapter-diagnostic-collect \
  SUBJECT=baseline \
  ENVIRONMENT=amelia-gtx1660-v1
```

Compare the reports:

```bash
make route-adapter-diagnostic-compare \
  BASELINE=.local-coder/qualifications/baseline-f3-adapter-v1-<timestamp>.json \
  CANDIDATE=.local-coder/qualifications/candidate-f3-adapter-v1-<timestamp>.json
```

The comparison rejects different protocols, fixtures, environments, commits, model
identities, route profiles, summaries, or attempt classifications. Its
`qualification_claim` remains `null`; the focused fixture still cannot replace Track G
real-task evidence.

The collected shared-adapter smoke comparison passed all five planner and five reviewer
attempts for both models. Qwythos therefore satisfies the production adapter contract on
that fixture, but used substantially more latency and completion tokens. That establishes
compatibility, not superior task quality.

## Remaining qualification work

Track G G0/G1 now freezes the varied real-task corpus described in
[`REAL_TASK_CORPUS.md`](REAL_TASK_CORPUS.md). The next qualification work is:

1. collect and compare the frozen G3.1 prompt-contract candidates documented in
   `QWYTHOS_PROMPT_TUNING.md`, using the measured G3 role generation settings;
2. select planner and reviewer prompts independently under the unchanged accuracy-first,
   no-material-regression policy;
3. open only the role-specific independently provisioned holdout cases permitted by that
   frozen gate;
4. measure cold startup and serial model-switch time; and
5. bind adapter, resource, lifecycle, development, and holdout evidence into one new
   versioned decision contract.

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
