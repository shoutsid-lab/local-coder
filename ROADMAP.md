# ROADMAP: Agent Skills + DSPy + GEPA Integration

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Active — primary implementation entry point

## 0. Why this document exists

This is the primary document to read before starting repository work, whether the actor
is a trusted service, a more capable model, or a human operator. The completed
recursive-improvement control-plane record lives in
[`docs/HANDOFF.md`](docs/HANDOFF.md); it is the stable baseline that this roadmap extends.

`local-coder` already has three of the exact pieces these projects formalize:

| local-coder today | Formalized by |
|---|---|
| `.local-coder/skills/*/SKILL.md` role procedures, loaded on demand | **Agent Skills** (agentskills.io open spec) |
| Hand-written prompts inside each skill/agent, glued together by `smolagents` | **DSPy** (signatures/modules instead of prompt strings) |
| `analyze-runs` → `create-campaign` → evaluator → scorecard → authorized promotion | **GEPA** (reflective, evolutionary prompt optimization with a trusted judge) |

This roadmap does not propose replacing the frozen architecture in `docs/ARCHITECTURE.md`. It proposes using each project to formalize a piece the repo has already hand-rolled, behind the same trust boundaries `AGENTS.md` and `docs/HANDOFF.md` already require: no automatic commits, no candidate-controlled evaluation, no unrestricted shell, no required cloud dependency for the core local loop.

Every phase below ends with `make verify` / `make agent-smoke` / `make handoff-check` staying green, per `AGENTS.md`.

---

## 1. Track A — Agent Skills (agentskills.io)

**Goal:** make `.local-coder/skills/*/SKILL.md` a spec-compliant, portable Agent Skill, and get local-coder listed as a skills-compatible client — without changing what a skill *does* in the runtime.

### A1. Spec-compliance pass (non-breaking)
- Add/validate frontmatter (`name`, `description`) on every existing skill under `.local-coder/skills/*/SKILL.md` against the [Agent Skills specification](https://agentskills.io/specification).
- Confirm the three-stage loading model (discovery → activation → execution) that `docs/ARCHITECTURE.md` already implies ("Role procedures live in `.local-coder/skills/*/SKILL.md`... This keeps role prompts reusable and prevents a large universal tool schema from consuming the small model's context") maps 1:1 onto the spec's progressive-disclosure model. Document the mapping in a new `docs/AGENT_SKILLS.md` rather than editing the protected `docs/ARCHITECTURE.md`.
- New file: `runtime/skills_loader.py` — loads only `name`/`description` from every skill at orchestrator startup (discovery), and defers reading the full `SKILL.md` body until the manager selects a role (activation), matching the spec exactly instead of the current implicit "load what the role needs" behavior.

### A2. Portable skill packaging
- Restructure skill directories to the standard shape (`SKILL.md`, optional `scripts/`, `references/`, `assets/`) so explorer/planner/implementer/repairer/reviewer skills can be dropped unmodified into any other skills-compatible agent (Claude Code, opencode, Goose, etc.) for cross-testing the prompts outside local-coder.
- Add a `make skills-lint` target that validates every skill folder against the spec (required frontmatter, no orphaned `references/`, description length limits) as a new, non-protected CI gate — separate from `make verify`, so it can fail without blocking core verification.

### A3. Registry participation (optional, operator-controlled)
- Publish the role skills (with hardware-specific instructions stripped, since they're 3B-model-tuned) to the `agentskills/agentskills` community index, and pull in vetted third-party skills (e.g. language-specific lint/test conventions) as *read-only reference material* for the planner/reviewer roles.
- Constraint: imported skills are data, never code. They can only add to a `SKILL.md` prompt body; they must not add new tools to `runtime/tools.py`'s allowlist. This preserves the "no unrestricted shell tool" boundary in `docs/ARCHITECTURE.md`.

**Deliverables:** `docs/AGENT_SKILLS.md`, `runtime/skills_loader.py`, `make skills-lint`, restructured `.local-coder/skills/` tree, zero changes to protected files.

---

## 2. Track B — DSPy (programming, not prompting)

**Goal:** replace hand-written prompt strings inside each role's `SKILL.md`/adapter with DSPy `Signature`/`Module` objects, so role behavior is a compiled program instead of static text — while keeping every existing trust boundary (native editor, LiteLLM aliases, worktrees, `make verify`) exactly as-is.

### B1. LM client wiring (no new inference dependency)
- DSPy talks to any OpenAI-compatible endpoint. Point `dspy.LM` at the **existing** LiteLLM gateway (`http://127.0.0.1:4000`) using the **existing** aliases:
  - `local-fast` → implementer / repairer signatures
  - `local-plan` → explorer / planner signatures
  - `local-review` → reviewer signature
- This adds zero new services. `llama.cpp`, LiteLLM, and the three stable route names stay frozen exactly as `AGENTS.md` requires ("Keep logical routes `local-fast`, `local-plan`, and `local-review` stable").

### B2. Signatures for each role (new module: `runtime/dspy_programs/`)
Define one `dspy.Signature` per existing role, matching the current adapter contracts described in `docs/ARCHITECTURE.md`:
- `ExplorerSignature` — read-only evidence adapter → structured findings (mirrors current explorer → `local-plan` adapter).
- `PlannerSignature` — evidence → atomic instruction (mirrors planner → `local-plan` adapter, and the existing task-plan JSON schema in `docs/TASK_PLANS.md`).
- `ImplementerSignature` / `RepairerSignature` — atomic instruction → strict JSON search/replace batch, i.e. exactly the schema `runtime/editor.py` already validates. DSPy produces the *content*; `runtime/editor.py` remains the **only** component authorized to write source, unchanged.
- `ReviewerSignature` — diff → structured review verdict (mirrors the fixed read-only review adapter).

Each signature is wrapped in a small `dspy.Module` (e.g. `dspy.ChainOfThought` or plain `dspy.Predict`, chosen per role's capability boundary from `docs/PIPELINE.md` — the 3B model profile is explicitly "unreliable for... multi-step semantic repair without external guidance", so implementer/repairer stay single-step `Predict`, while explorer/planner can use `ChainOfThought`).

### B3. Adapter boundary, not orchestrator replacement
- `smolagents` remains the orchestrator/manager (`docs/ARCHITECTURE.md`'s frozen runtime flow is unchanged). DSPy modules become swappable implementations *behind* the same adapters smolagents already calls — this is an internal refactor of "how a prompt is built and parsed," not a new orchestration layer, so it does not conflict with the "do not redirect the project into a generic CLI wrapper" constraint in `AGENTS.md`.
- The strict-JSON validation currently in `runtime/editor.py` becomes the DSPy signature's output type (e.g. a Pydantic-typed output field), so schema drift is caught before the editor even sees a malformed batch — a strict improvement, not a relaxation, of the existing "validates approved paths and unique exact matches in memory" guarantee.

**Deliverables:** `runtime/dspy_programs/{explorer,planner,implementer,repairer,reviewer}.py`, `runtime/dspy_lm.py` (LiteLLM-backed `dspy.LM` factory), `docs/DSPY_INTEGRATION.md`, updated `requirements-agent.txt` (add `dspy`), new non-protected tests under `tests/test_dspy_programs.py`.

**Explicitly out of scope for this track:** any change to `docs/ARCHITECTURE.md`, `docs/PIPELINE.md`, or the native editor's write authority — these are protected files and architectural invariants; changing them requires the maintainer's explicit sign-off per `AGENTS.md`, not just this roadmap.

---

## 3. Track C — GEPA (optimizing the DSPy programs)

**Goal:** stop hand-tuning role instructions by trial and error, and instead let `dspy.GEPA` evolve them — but run every optimization *inside* the repo's completed trusted-evaluator/campaign machinery (`evaluation/`, `docs/HANDOFF.md`, `docs/RECURSIVE_IMPROVEMENT.md`), so a GEPA-proposed prompt is subject to the exact same holdout/promotion boundary as a candidate code change.

### C1. Feedback source: the audit trail you already have
- `.local-coder/state/agent.db` already records every run, tool call, artifact, and verification result. This is precisely the "trace of the program's execution" GEPA's reflection step consumes.
- Build `runtime/dspy_programs/gepa_dataset.py` to turn `analyze-runs` output (already read-only, already hash-stamped per `docs/RECURSIVE_IMPROVEMENT.md`'s "`analyze-runs` opens SQLite read-only and emits hashes and structured facts") into DSPy `Example` objects: (task, role, evidence) → (output, pass/fail from `make verify`, reviewer verdict text).
- The reviewer's existing structured verdict becomes GEPA's textual feedback signal directly — this is exactly the `{'score': float, 'feedback': str}` shape `dspy.GEPA`'s metric function expects, and it already exists in this repo's review adapter.

### C2. Where GEPA runs: a new campaign kind, not a new trust path
- Add `optimize-prompts` as a **new campaign type** alongside the existing `create-campaign` / `approve-brief` / `build-candidate` / `evaluate` / `record-decision` / `close-campaign` / `audit-campaign` commands in `docs/RECURSIVE_IMPROVEMENT.md` — reusing every existing boundary:
  1. `create-campaign --kind prompt-optimization` mines one bounded brief from run history (e.g. "planner instructions underperform on multi-file discovery tasks").
  2. An authorized actor approves the brief (unchanged step).
  3. `build-candidate` runs `dspy.GEPA` offline, against the frozen dev/holdout split already defined for code campaigns, producing a candidate *signature/instruction set* (a JSON program state) instead of a candidate commit.
  4. `evaluate` runs the **same** `evaluation/supervisor.py` bubblewrap sandbox, base-owned contracts, and holdout oracles — the candidate prompts are just another artifact type being scored, not a new execution path.
  5. `record-decision` / `close-campaign` / `audit-campaign` are unchanged. Promotion still cannot be delegated to the candidate under evaluation.
- This means GEPA never touches `.local-coder/holdout/`, never sees oracle data, and never decides its own promotion — identical to the existing candidate-code boundary in `docs/RECURSIVE_IMPROVEMENT.md`.

### C3. Reflection LM: keep it optional and operator-controlled
- GEPA's *reflection* LM (the model that reads failures and proposes new instructions) is allowed to be a stronger model than the 3B `local-coder` alias — but only on the **trusted-evaluator side**, run by the operator outside any candidate worktree, exactly like the existing "trusted evaluator... more capable model" allowance already written into `docs/PIPELINE.md`'s Recursive Improvement Pipeline section ("The actor may be a trusted service or more capable model, but not the candidate under evaluation").
- This keeps `AGENTS.md`'s "Do not introduce Claude or a required cloud model dependency" intact: the *runtime* stack (explorer/planner/implementer/repairer/reviewer) still only ever needs `local-fast`/`local-plan`/`local-review`. A cloud or larger reflection LM is strictly opt-in tooling for the operator's offline optimization runs, never a dependency of `./local-coder.py run`.

**Deliverables:** `runtime/dspy_programs/gepa_dataset.py`, `runtime/dspy_programs/gepa_runner.py`, new `optimize-prompts` campaign kind wired through the existing `local-coder.py` campaign subcommands, `docs/GEPA_OPTIMIZATION.md`, extension of `docs/RECURSIVE_IMPROVEMENT.md`'s scorecard schema to include a "prompt-candidate" artifact type (requires maintainer sign-off, since that file is protected).

---

## 4. Phased delivery plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **0 — Groundwork** | `docs/AGENT_SKILLS.md`, `docs/DSPY_INTEGRATION.md`, `docs/GEPA_OPTIMIZATION.md` drafted; add `dspy`, `gepa` to `requirements-agent.txt`; `make skills-lint` scaffold | `make verify` and `make agent-smoke` still pass unchanged |
| **1 — Agent Skills compliance** | A1 + A2 from Track A | Every skill under `.local-coder/skills/` passes `make skills-lint`; no behavior change to running agents |
| **2 — DSPy signatures behind adapters** | B1 + B2 + B3, one role at a time (reviewer first — read-only, lowest blast radius; implementer/repairer last — they hold write authority) | Each role's DSPy module produces output that `runtime/editor.py` and the existing verification pipeline accept unmodified; `make agent-smoke` green after each role migrates |
| **3 — GEPA dataset + offline optimization** | C1, run entirely offline against exported run history, no runtime wiring yet | A `gepa_dataset.py` export from real `.local-coder/state/agent.db` history produces a valid DSPy training/dev/holdout split |
| **4 — GEPA inside the campaign system** | C2 + C3 | An `optimize-prompts` campaign runs end-to-end through `create-campaign → approve-brief → build-candidate → evaluate → record-decision`, producing an auditable, promotable prompt candidate with no change to the promotion-authority boundary |

Each phase is independently revertible: DSPy modules can be swapped back for the current hand-written prompts without touching the editor, worktree, or verification layers, and GEPA campaigns are additive to `docs/RECURSIVE_IMPROVEMENT.md`, not a replacement of the code-candidate path.

---

## 5. Explicit non-goals

- No change to which component may write source (`runtime/editor.py` only, unchanged).
- No change to the three LiteLLM route names or the requirement to run entirely on the current GTX 1660 Ti / 8 GiB profile for the core `run`/`repair`/`review` path.
- No automatic commits, merges, or promotions introduced anywhere in Tracks A–C.
- No relaxation of protected files or protected contract tests. `ROADMAP.md` and
  `docs/HANDOFF.md` are trusted planning and completion records; protected-file changes
  remain explicit, reviewable actions rather than candidate-controlled edits.

## 6. Open questions for the primary actor

1. Should Phase 2's DSPy migration happen role-by-role in separate PRs (safer, matches the "narrowly scoped" convention in `AGENTS.md`) or as one coordinated change?
2. Should the `optimize-prompts` campaign kind live in the same `evaluation/` module tree as code campaigns, or in a sibling `evaluation/prompt_optimization/` package to keep the protected `evaluation/` contract tests scoped to code evaluation only?
3. What's the acceptable reflection-LM choice for offline GEPA runs — a larger local model loaded on-demand (the disabled `local-deep` profile mentioned in `docs/ARCHITECTURE.md`), or an operator-supplied API key strictly outside the committed repo?
