# GEPA Prompt Campaigns

C2.1 makes an offline GEPA result a first-class campaign candidate. C2.2 adds
trusted paired prompt evaluation. Track D adds separate authorized activation, rollback,
and unattended lifecycle completion without changing optimization behavior.

## Boundary

A `prompt-optimization` campaign reuses the existing bounded lifecycle:

```text
create-campaign -> approve-brief -> build-candidate -> evaluate
                -> record-decision -> close-campaign -> audit-campaign
```

Campaign creation freezes the development suite, evaluator environment, GEPA dataset
hashes, selected role, model routes, approximate GEPA metric target, hard campaign model
call limit, reflection limits, unsafe-proposal limit, seed, and rollback condition. An
external evaluator holdout may also be frozen at creation time. When it is omitted,
the brief records a deferred holdout and paired evaluation remains blocked until the
operator supplies one. New campaigns also freeze the trusted prompt-evaluator hash at
creation. C2.1 campaigns created before that field existed bind the evaluator hash exactly
once before their first evaluation. The approved brief permits no source-file edits.

`build-candidate` invokes the existing offline GEPA runner. It writes immutable output
under `.local-coder/gepa-campaigns/` and records one hash-bound `prompt_candidate`
artifact in SQLite. The build has no agent run, Git branch, source worktree, activation,
or promotion action.

Paired prompt evaluation and scorecard lineage are implemented. Evaluation archives the
inert DSPy program state and trusted evaluator identity but performs no activation.
Deployment remains separate: only a promoted evaluation from a cleanly closed campaign may
be atomically activated, and every activation or rollback is archived independently.

## Create a campaign

An external evaluator holdout is optional at campaign creation. Omitting it creates a
campaign with an explicitly deferred evaluation holdout; paired evaluation still requires
an operator-controlled external rotation.

```bash
./local-coder.py create-campaign \
  --kind prompt-optimization \
  --baseline . \
  --dataset .local-coder/gepa-datasets/planner-seed-v1 \
  --role planner \
  --reflection-route local-plan \
  --prompt-target-metric-calls 60 \
  --prompt-max-unsafe-proposals 3 \
  --prompt-no-improvement-patience 6 \
  --prompt-reflection-max-tokens 512 \
  --prompt-max-instruction-chars 1600 \
  --prompt-allow-perfect-only \
  --rollback-condition 'Any development or holdout regression.'
```


To freeze an external holdout immediately, first provision it with `rotate-holdout`,
then add both printed paths to `create-campaign`:

```bash
  --holdout-suite .local-coder/holdout/ROTATION/manifest.json \
  --holdout-oracle .local-coder/holdout/ROTATION/oracle.json
```

Supplying only one holdout path fails closed.

The command returns a campaign ID and one pending brief. Review the frozen metadata, then
record approval:

```bash
./local-coder.py approve-brief BRIEF_ID \
  --actor trusted-reviewer \
  --rationale 'Dataset, budget, role, and rollback condition are bounded.'
```

## Build the inert candidate

```bash
./local-coder.py build-candidate CAMPAIGN_ID
```

The command reserves stdout for one complete JSON document. DSPy/GEPA progress,
metric summaries, and logging are routed to stderr, so piping stdout through `tee` or
`jq` remains reliable during a live optimization run.

The output includes:

- the candidate-build ID;
- the complete offline GEPA result;
- a typed `prompt_candidate` artifact containing dataset, instruction, report, candidate,
  and manifest hashes; and
- explicit `activation: not_performed` and `promotion: not_performed` fields.

Inspect stored lineage read-only through the existing campaign state APIs. Prompt builds
use three explicit terminal outcomes:

- `candidate_ready`: a safe, strictly improved instruction was selected;
- `candidate_rejected`: GEPA proposed a changed instruction but it failed safety or budget
  policy; and
- `no_improvement`: the selected program remains the baseline without a rejected winning
  proposal.

Only `candidate_ready` is eligible for paired evaluation. Every prompt build has
`build_kind: prompt-optimization`, no `run_id`, and exactly one `prompt_candidate`
artifact. Rejected and null-result candidates are blocked before external holdout files
are loaded.

## Run paired prompt evaluation

Provision a `prompt-replay` holdout using the schema in
[`PROMPT_HOLDOUT.md`](PROMPT_HOLDOUT.md), then run:

```bash
./local-coder.py evaluate \
  --campaign-id CAMPAIGN_ID \
  --build-id BUILD_ID \
  --holdout-suite .local-coder/holdout/ROTATION/manifest.json \
  --holdout-oracle .local-coder/holdout/ROTATION/oracle.json
```

Prompt evaluation derives baseline and candidate instruction state from campaign lineage,
so source-worktree flags are rejected. It replays the frozen development split and the
external holdout, records paired cases in SQLite, and emits the standard ordered
safety-through-efficiency scorecard. Exact holdout case scores, observation hashes, and
oracle outputs remain redacted from stdout. A deferred campaign binds to its first
external holdout identity exactly once; subsequent holdout or evaluator mismatches fail
closed.

An eligible result may receive an explicit decision without activating the prompt:

```bash
./local-coder.py record-decision EVALUATION_ID promote \
  --actor trusted-reviewer \
  --rationale 'All predeclared prompt gates passed.'
```

For unattended completion after candidate construction, use:

```bash
scripts/run-prompt-lifecycle.sh \
  CAMPAIGN_ID BUILD_ID /path/manifest.json /path/oracle.json \
  "Chief Scoop Officer" --activate
```

The script runs or reuses evaluation, derives `promote` or `reject` from the frozen
scorecard, closes and audits the campaign, and activates only when promotion is valid.
See [`PROMPT_DEPLOYMENT.md`](PROMPT_DEPLOYMENT.md) for explicit activation, inspection,
and rollback commands.

## Fail-closed behavior

Creation or build fails when:

- the dataset hash or manifest changes after brief creation;
- the dataset references trusted holdout or oracle paths;
- source-candidate arguments or overlays are supplied to a prompt campaign;
- the GEPA output files or hashes disagree;
- role or dataset identity differs from the approved brief;
- optimizer lineage hashes are incomplete;
- accepted, proposed, and selected instruction state contradict one another;
- a rejected or null-result build is submitted for paired evaluation;
- external prompt manifest, role, oracle, or case identity differs;
- the candidate state or instruction hash changes after build;
- a deferred campaign is rebound to a different holdout; or
- GEPA reports activation or promotion.

## Candidate outcome integrity

The campaign artifact describes the selected program, not merely the best proposal GEPA
considered. It records separate proposed and selected instruction hashes and booleans. A
rejected proposal may therefore have `proposed_candidate_changed: true` while
`selected_candidate_changed`, `candidate_changed`, and `candidate_accepted` are all
false. The persisted candidate file and `candidate_instruction_hash` must then match the
baseline. Metric-call targets, actual metric calls, target overrun, and hard model-call
accounting are copied into the campaign artifact for later scorecard auditing.
