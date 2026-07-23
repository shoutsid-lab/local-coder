# GEPA Prompt Campaigns

C2.1 makes an offline GEPA result a first-class campaign candidate without adding any
runtime activation or promotion authority.

## Boundary

A `prompt-optimization` campaign reuses the existing bounded lifecycle:

```text
create-campaign -> approve-brief -> build-candidate
```

Campaign creation freezes the development suite, evaluator environment, GEPA dataset
hashes, selected role, model routes, metric-call budget, reflection limits, seed, and
rollback condition. An external evaluator holdout may also be frozen at creation time.
When it is omitted during this C2.1 build-only slice, the brief records that paired prompt
evaluation remains blocked until an external holdout is supplied by the later evaluation
slice. The approved brief permits no source-file edits.

`build-candidate` invokes the existing offline GEPA runner. It writes immutable output
under `.local-coder/gepa-campaigns/` and records one hash-bound `prompt_candidate`
artifact in SQLite. The build has no agent run, Git branch, source worktree, activation,
or promotion action.

Paired prompt evaluation, scorecard integration, decision lineage, and activation remain
later campaign slices.

## Create a campaign

An external evaluator holdout is optional for this build-only slice. Omitting it creates
a campaign with an explicitly deferred evaluation holdout; source campaigns and actual
paired evaluation still require an operator-controlled external rotation.

```bash
./local-coder.py create-campaign \
  --kind prompt-optimization \
  --baseline . \
  --dataset .local-coder/gepa-datasets/planner-seed-v1 \
  --role planner \
  --reflection-route local-plan \
  --prompt-max-metric-calls 60 \
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

The output includes:

- the candidate-build ID;
- the complete offline GEPA result;
- a typed `prompt_candidate` artifact containing dataset, instruction, report, candidate,
  and manifest hashes; and
- explicit `activation: not_performed` and `promotion: not_performed` fields.

Inspect stored lineage read-only through the existing campaign state APIs. A successful
prompt build has status `candidate_ready`, `build_kind: prompt-optimization`, no `run_id`,
and exactly one `prompt_candidate` artifact.

## Fail-closed behavior

Creation or build fails when:

- the dataset hash or manifest changes after brief creation;
- the dataset references trusted holdout or oracle paths;
- source-candidate arguments or overlays are supplied to a prompt campaign;
- the GEPA output files or hashes disagree;
- role or dataset identity differs from the approved brief;
- optimizer lineage hashes are incomplete; or
- GEPA reports activation or promotion.
