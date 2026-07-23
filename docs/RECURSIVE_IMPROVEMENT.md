# Recursive Improvement Operations

## Safety model

Recursive improvement changes generations of the harness, never the running generation
in place. The trusted checkout owns normalization, manifests, contract workers, oracles,
budgets, and scorecards. Candidate source is mounted read-only and without network access.
The audit database records recommendations and authorization decisions but performs no Git action.

## 1. Analyze evidence

```bash
./local-coder.py analyze-runs --limit 20
```

The command opens SQLite in read-only mode. Historical gaps remain JSON `null`/`unknown`;
they are never converted to zero. Raw task, tool, and model text is hashed or classified
before it can influence a brief.

Before creating a campaign, provision an operator-controlled holdout rotation from files
that are outside this repository:

```bash
./local-coder.py rotate-holdout 2026-07-rotation \
  --manifest /secure/input/manifest.json \
  --oracle /secure/input/oracle.json
```

The command validates both files, copies them with restrictive permissions into ignored
`.local-coder/holdout/` storage, and never overwrites an existing rotation. Use the two
paths it prints in every campaign command. The repository contains no production holdout
or oracle material.

## 2. Create and approve one brief

Use evidence run IDs, a clean baseline checkout, explicit editable files, a predeclared
target case, and a rollback condition:

```bash
./local-coder.py create-campaign \
  --baseline /path/to/baseline \
  --run-id RUN_ID \
  --allowed-file runtime/editor.py \
  --target-case missing-match \
  --rollback-condition "Any safety or holdout regression" \
  --holdout-suite .local-coder/holdout/2026-07-rotation/manifest.json \
  --holdout-oracle .local-coder/holdout/2026-07-rotation/oracle.json

./local-coder.py approve-brief BRIEF_ID \
  --actor "review-model" \
  --rationale "The bounded hypothesis and file scope are acceptable."
```

Only one brief is allowed per campaign. A campaign permits one candidate until the audit
store contains ten completed campaigns with no safety or regression failure; the hard
ceiling then becomes three.

A prompt campaign uses the same approval record but freezes a GEPA dataset, one DSPy role,
optimization and model-call budgets, a trusted prompt-evaluator hash, and either an
external `prompt-replay` holdout identity or an explicit deferred holdout. See
[`GEPA_CAMPAIGNS.md`](GEPA_CAMPAIGNS.md).

## 3. Build a candidate

Prompt and skill ideas can be represented by `ExperimentOverlay` in memory. Source
changes must use the existing isolated worktree and native atomic editor. The evaluator
does not create, commit, merge, push, delete, or promote a candidate. An authorized actor
must inspect and commit an experiment before generational comparison. The actor may be a
trusted service or more capable model independent from the candidate.

```bash
./local-coder.py build-candidate CAMPAIGN_ID \
  --overlay "planner_handoff=Use explicit evidence and acceptance fields"
```

This service-dependent command derives its task only from the approved structured brief,
creates one normal agent worktree, and records the build lineage. It never commits the
result. A failed build consumes the bounded attempt rather than retrying indefinitely.

For `prompt-optimization`, `build-candidate` instead runs bounded offline GEPA and records
one hash-bound `prompt_candidate` artifact. The artifact points to an inert DSPy JSON
program state and has no run, branch, worktree, activation, or promotion side effect. Only
a changed, accepted `candidate_ready` result may enter evaluation.

## 4. Run paired evaluation

```bash
./local-coder.py evaluate \
  --campaign-id CAMPAIGN_ID \
  --build-id BUILD_ID \
  --baseline /path/to/clean/baseline \
  --candidate /path/to/clean/candidate \
  --target-case missing-match \
  --repetitions 2 \
  --holdout-suite .local-coder/holdout/2026-07-rotation/manifest.json \
  --holdout-oracle .local-coder/holdout/2026-07-rotation/oracle.json
```

The supervisor verifies manifest, oracle, commit, environment, and budget identity before
execution. It also requires the candidate path to be the exact worktree recorded for the
named build. It runs baseline then candidate under the same configuration. Each repetition
includes candidate-owned `make verify`, base-owned development contracts, and separately
mounted holdout contracts. Timeouts, nonzero exits, malformed observations, and output
limits are terminal recorded case results; there is no retry loop.

Campaign creation freezes the combined development/holdout manifest hash, the holdout
manifest-plus-oracle hash, and the evaluator environment hash. Campaign evaluation rejects
any mismatch before candidate execution. Campaigns created by an older schema without
those identities remain readable but are not evaluable.

Every sandbox command runs as an unprivileged UID with all capabilities dropped. A
base-owned wrapper installs an immutable kernel `RLIMIT_NPROC` before executing candidate
code, so descendants cannot evade the declared process ceiling. Candidate construction
also shares one call and token budget across all logical model routes; missing usage or
an exhausted limit terminates the build and remains visible in SQLite.

When `--campaign-id` is omitted, `--allowed-file` must be supplied at least once. Campaign
evaluations derive allowed paths from the approved brief and reject a conflicting CLI
scope. Any protected or undeclared changed path fails the first safety gate.

The scorecard is ordered and non-scalar: safety, correctness, regression, control,
improvement, then efficiency. Failure at an earlier gate cannot be traded for an
efficiency or target-metric gain. Promotion authorization is recorded separately from
the technical scorecard. Campaign control and efficiency gates incorporate
the recorded build trajectory, including rejected edits, tool failures, bounded retries,
terminal status, fresh review, and model usage.

Prompt candidates use the same ordered gates and campaign records through a dedicated
base-owned replay adapter. The adapter loads baseline and candidate instruction states,
replays the frozen development split and a separately provisioned external holdout, and
archives paired cases, the candidate program state, and the prompt-evaluator identity.
Source-worktree flags are rejected for this path. Exact holdout case scores, outputs, and
observation hashes remain redacted from CLI stdout. Older C2.1 prompt campaigns bind the
prompt-evaluator hash once before their first evaluation; new campaigns freeze it at
creation. Neither evaluation path activates or promotes its candidate.

## 5. Record the authorization decision

```bash
./local-coder.py record-decision EVALUATION_ID reject \
  --actor "review-model" \
  --rationale "The predeclared improvement gate did not pass."

./local-coder.py close-campaign CAMPAIGN_ID
./local-coder.py show-campaign CAMPAIGN_ID
./local-coder.py audit-campaign CAMPAIGN_ID
```

Recording `promote` does not alter Git. An authorized actor must independently commit,
merge, or otherwise promote an accepted candidate. Worktree retention and cleanup remain
explicit operations.

`audit-campaign` opens SQLite read-only and fails closed unless the campaign has one
approved brief, bounded build lineage, frozen suite/holdout/environment identity, paired
case evidence, ordered scorecards, one authorization decision per evaluation, and a
terminal status consistent with safety and regression evidence. Source evaluations require
hash-valid candidate patch and trajectory artifacts. Prompt evaluations require a
hash-valid inert program state and prompt-evaluator identity bound to the campaign.
