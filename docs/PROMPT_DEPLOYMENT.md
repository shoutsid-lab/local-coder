# Prompt Deployment and Rollback

Prompt optimization and evaluation produce inert DSPy program states. Deployment is a
separate operator-authorized step. A prompt state can become active only when its campaign
closed `completed_clean`, its paired evaluation completed, its scorecard permits promotion,
and its recorded decision is `promote`.

## One-command lifecycle after candidate construction

After `build-candidate` has produced `candidate_ready`, run:

```bash
scripts/run-prompt-lifecycle.sh \
  CAMPAIGN_ID \
  BUILD_ID \
  /absolute/path/manifest.json \
  /absolute/path/oracle.json \
  "Chief Scoop Officer" \
  --activate
```

The script runs or reuses paired evaluation, derives the only scorecard-consistent decision,
closes and audits the campaign, and activates only an eligible candidate. A valid rejection
is finalized and reported without activation. Evidence is retained under
`.local-coder/prompt-lifecycle-runs/`.

Omit `--activate` to complete the entire decision, close, and audit lifecycle while leaving
runtime behavior unchanged.

## Explicit commands

Finalize an already evaluated campaign:

```bash
./local-coder.py finalize-prompt-campaign CAMPAIGN_ID \
  --actor "Chief Scoop Officer" \
  --rationale "Decision derived from the frozen paired scorecard." \
  --activate
```

Activate a previously promoted evaluation separately:

```bash
./local-coder.py activate-prompt EVALUATION_ID \
  --actor "Chief Scoop Officer" \
  --rationale "Deploy the promoted prompt state."
```

Inspect active states:

```bash
./local-coder.py show-active-prompts
```

Rollback a role to its previous authorized state, or to the code baseline when no previous
activation exists:

```bash
./local-coder.py rollback-prompt planner \
  --actor "Chief Scoop Officer" \
  --rationale "Runtime regression observed after activation."
```

## Storage and runtime loading

Activation copies the exact hash-verified `candidate.json` into the operator-owned store:

```text
.local-coder/prompt-programs/
  active/ROLE.json
  history/ACTIVATION_ID/program.json
  history/ACTIVATION_ID/metadata.json
```

The active role pointer is replaced atomically. Each DSPy runtime adapter checks its role
pointer before inference, verifies that the state remains inside the history store, verifies
the SHA-256 hash, and then calls DSPy's `load`. Missing state means the code-defined baseline
continues to run. Malformed, missing, escaped, or hash-mismatched active state fails closed.

Activation and rollback events are archived as hash-bound evaluation artifacts. Candidate
artifacts remain inert and continue to state `activation: not_performed`; deployment is a
separate trusted action after evaluation and authorization.

## Authority boundary

The candidate cannot activate itself. `build-candidate` and `evaluate` do not write the
active store. `record-decision promote` alone also does not activate anything. Activation
requires a separate command after the campaign has closed cleanly, and rollback always
requires an actor and rationale.
