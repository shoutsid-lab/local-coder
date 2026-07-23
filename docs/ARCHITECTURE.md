# Local Coder Architecture

`local-coder` is a resource-bounded local coding-agent system. It combines a
role-separated agent runtime with deterministic editing and verification, persistent
evidence, bounded source and prompt improvement campaigns, external holdout evaluation,
and operator-authorized prompt deployment.

The architecture depends on stable model routes and explicit trust boundaries rather than
one physical model, GPU, or inference server.

## System boundaries

The repository contains four cooperating planes:

1. **Agent runtime** — gathers evidence, plans, edits, repairs, and reviews code.
2. **Deterministic control plane** — validates edits, runs verification, records evidence,
   and enforces resource limits.
3. **Improvement control plane** — constructs and evaluates isolated source candidates or
   inert prompt candidates.
4. **Prompt deployment plane** — activates or rolls back an authorized prompt state
   without granting deployment authority to the candidate.

```text
Developer or trusted operator
        │
        ├── agentic coding ───────→ isolated code-editing run
        │
        ├── source campaign ──────→ isolated source candidate
        │
        └── prompt campaign ──────→ inert DSPy candidate state
                                        │
                                        ▼
                              trusted evaluation and decision
                                        │
                                        ▼
                         optional authorized prompt deployment
```

Source-code promotion remains outside the runtime. The system does not commit, merge, or
push candidate source code.

## Agentic code-editing runtime

```text
Developer
   ↓
local-coder.py run
   ↓
smolagents CodeAgent orchestrator
   ├── explorer      → read-only evidence adapter → local-plan
   ├── planner       → read-only evidence adapter → local-plan
   ├── implementer   → code-action leaf → local-fast → validated exact edits
   ├── repairer      → code-action leaf → local-fast → validated exact edits
   └── reviewer      → fixed read-only review adapter → local-review
        ↓
LiteLLM route aliases
        ↓
OpenAI-compatible local inference endpoint
```

Each **code-editing run** receives an isolated Git worktree. Agents can only use the
narrow tools exposed by `runtime/tools.py`; there is no unrestricted shell tool. Source
edits pass through `runtime/editor.py`, which requests strict JSON search/replace
operations, validates approved paths and unique exact matches in memory, and writes
nothing unless the complete batch is valid.

Formatting, linting, compilation, tests, protected contracts, and review evidence remain
deterministic and authoritative. Model output can propose a change, but it cannot bypass
the editor or verification boundaries.

## Skills and model routing

Role procedures live in `.local-coder/skills/*/SKILL.md`. Each skill selects its logical
model route, tool allowlist, and maximum steps. This keeps prompts role-specific and avoids
exposing a large universal tool schema to a small local model.

Stable LiteLLM aliases separate architecture from physical inference. Planning, editing,
and review routes may currently resolve to one model, but a route can later point to a
different local model or server without changing agent contracts.

## Persistent state and evidence

`.local-coder/state/agent.db` is the control-plane record. It stores or binds:

- runs, agents, tool calls, artifacts, verification results, and model metrics;
- campaigns, approved briefs, candidate builds, and candidate artifacts;
- evaluator and holdout identities;
- paired development and holdout cases;
- scorecards, decisions, campaign closure, and audit lineage; and
- prompt activation and rollback evidence.

Generated databases, run artifacts, candidate worktrees, holdout rotations, optimized
prompt states, and deployment stores are ignored by Git. Hashes bind persisted records to
the artifacts and evaluator identities used to produce them.

Completed campaign lineage is checked through the read-only `audit-campaign` path. Audit
verifies bounded builds, paired evidence, archived artifact hashes, scorecard ordering,
authorization decisions, and deployment lineage without invoking Git or mutating SQLite.

## Source-candidate improvement path

Source improvement is generational rather than online self-modification:

```text
normalized audit evidence
        ↓
approved bounded brief
        ↓
committed source candidate in an isolated worktree
        ↓
networkless baseline and candidate sandboxes
        ↓
base-owned development and external holdout contracts
        ↓
ordered scorecard
        ↓
independent decision record
```

`evaluation/supervisor.py` uses bubblewrap to expose only required runtime inputs to
base-owned contract workers. Candidate-owned verification receives a read-only checkout,
an ephemeral size-bounded `/tmp`, no network, and the trusted Python environment.
Sandboxed commands run under an unprivileged UID with capabilities dropped, while the
base-owned process guard installs a kernel process-count ceiling before candidate code
executes.

The trusted evaluator runs outside candidate worktrees. A candidate cannot alter its
brief, evaluator, contracts, holdout cases, scorecard ordering, or promotion policy.
Evaluation produces a recommendation; a separate trusted actor records the decision.

## Prompt-candidate improvement path

Prompt optimization is parallel to source improvement but does not use a worktree. The
candidate is an inert, hash-bound DSPy program state:

```text
frozen replay dataset and approved prompt brief
        ↓
bounded GEPA optimization
        ↓
inert candidate.json plus build evidence
        ↓
paired baseline and candidate replay
        ├── frozen development split
        └── independent external holdout
        ↓
ordered scorecard
        ↓
independent decision and campaign close
```

Candidate construction records explicit terminal outcomes:

- `candidate_ready` — a changed candidate passed construction controls;
- `candidate_rejected` — a proposal existed but the baseline remained selected; and
- `no_improvement` — no eligible improvement was found.

Only `candidate_ready` can enter paired evaluation. Construction cannot activate a
prompt, read the external promotion holdout, or record its own decision.

Prompt evaluation replays the code-defined baseline and the inert candidate through the
same typed role contract. It enforces ordered safety, correctness, holdout-regression,
resource-control, development-improvement, and efficiency gates. Aggregate improvement
cannot override an earlier failed gate.

## Holdout isolation and identity freezing

Production holdout manifests and oracles are provisioned from an external
operator-controlled source into ignored `.local-coder/holdout/<rotation>/` storage.
Campaign commands reject candidate-visible Git paths.

Source and prompt campaigns bind holdouts differently:

- **Source campaigns** freeze the selected evaluator environment and holdout identity as
  part of the approved campaign inputs.
- **Prompt campaigns** keep the external holdout unavailable during optimization. The
  validated manifest-plus-oracle identity is bound at the first paired evaluation and is
  immutable afterward. New prompt campaigns can also freeze the evaluator identity at
  creation; compatible older campaigns bind it exactly once before evaluation.

Evaluation fails closed if a frozen artifact, evaluator, program state, manifest, oracle,
or environment identity changes. Oracle outputs and exact holdout evidence are not
returned through the normal command output.

## Decision, prompt deployment, and rollback

Evaluation and deployment are separate authority domains:

```text
paired evaluation
   ↓
scorecard recommendation
   ↓
trusted decision record
   ↓
clean campaign close and audit
   ↓
optional activate-prompt
   ↓
hash-verified active role state
```

A prompt state can become active only after an eligible scorecard, an explicit `promote`
decision, a clean campaign close, and successful lineage checks. A rejected candidate
remains inert.

`evaluation/prompt_deployment.py` copies the exact authorized program state into trusted
history storage and atomically replaces the active role pointer. `runtime/prompt_activation.py`
resolves a role to either:

1. its code-defined baseline instruction; or
2. a deployed state whose location and SHA-256 hash verify against trusted history.

Malformed, missing, escaped, or hash-mismatched active state fails closed. Rollback is an
independent trusted action that restores the previous authorized deployment or the
code-defined baseline. Candidate construction, evaluation, and decision recording cannot
write the active prompt store.

Operator procedures are documented in [`PROMPT_DEPLOYMENT.md`](PROMPT_DEPLOYMENT.md),
while holdout schemas and provisioning rules are documented in
[`PROMPT_HOLDOUT.md`](PROMPT_HOLDOUT.md).

## Hardware adaptation

The current development profile is resource constrained and may route several logical
roles to one quantized local coding model. That is a deployment profile, not an
architectural invariant.

The architecture requires only:

- OpenAI-compatible model endpoints behind stable logical aliases;
- route-specific token and call budgets;
- serialized or otherwise resource-safe local inference; and
- deterministic control-plane behavior independent of model size.

Larger or specialized local models can replace a route when hardware permits. They do not
change the editor, evaluator, campaign, audit, deployment, or rollback contracts.

## Architectural invariants

1. A model may propose work but cannot bypass deterministic editing or verification.
2. Code-editing runs and source candidates are isolated; prompt candidates remain inert
   program-state artifacts.
3. Candidates cannot control briefs, evaluators, holdouts, scorecard ordering, decisions,
   activation, or rollback.
4. External holdout evidence is never optimization input for the candidate it evaluates.
5. Resource limits and missing accounting fail closed.
6. Evaluation recommends; a separate trusted action decides.
7. Source promotion remains external to the runtime.
8. Prompt activation requires promotion, clean closure, audit-consistent lineage, and
   hash-verified trusted storage.
9. Rejected candidates cannot alter active runtime behavior.
10. Every persistent transition is inspectable through immutable or hash-bound evidence.

The direct reviewer and native repair CLI remain available as focused debugging utilities.
They use the same read-only review and validated native editor boundaries as the agent
runtime.
