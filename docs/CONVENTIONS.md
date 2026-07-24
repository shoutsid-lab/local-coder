# Coding conventions

- Make only changes required by the request.
- Do not modify unrelated code.
- Add or update tests for behavioural changes.
- Use Python type hints.
- Use concise docstrings for public functions.
- Prefer clear, conventional code over clever code.
- Preserve existing APIs unless explicitly asked to change them.
- Run the test suite after making changes.
- Treat evaluation manifests, oracles, contracts, and promotion policy as trusted
  controls rather than candidate-editable implementation surfaces.
- Record improvement hypotheses and acceptance metrics before running candidates.
- Use canonical JSON hashes for suite, oracle, environment, overlay, and outcome
  identities.
- Keep holdout observations redacted from CLI reports and never mount trusted oracle
  files into the candidate contract sandbox.
- Record brief approvals and promotion decisions with an actor and rationale; do not
  infer authorization from the candidate model response or a decision record.

## Capability and investment conventions

- Prefer direct integration of established infrastructure over building a smaller custom
  substitute.
- Use focused tests and operational checks to catch regressions, stale state, path escape,
  and resource failures.
- Do not require a new campaign, holdout, or extensive comparative programme before adding
  an established local capability that directly addresses an active roadmap item.
- Use comparative evaluation when choosing between uncertain model, prompt, or policy
  candidates, not as a universal prerequisite for ordinary engineering work.
- Do not treat synthetic smoke fixtures as primary claims about real coding performance.
- Prefer capability improvements over another control-plane abstraction.

## Roadmap conventions

- Keep root [`ROADMAP.md`](../ROADMAP.md) as the repository-wide active queue and index;
  do not replace it with a detailed programme plan.
- Put detailed multi-phase programme roadmaps under `docs/roadmaps/` and link them from
  the root roadmap and documentation index.
- Use descriptive programme names. Track letters are optional, and existing indexed
  labels must not be reused where that would create ambiguity.
- Check `ROADMAP.md`, `docs/roadmaps/`, and [`HISTORY.md`](HISTORY.md) before naming a
  programme. Do not require Git-history archaeology for routine label allocation.
- Treat root identifiers such as `R1` and `S1` as queue identifiers, not programme-track
  labels.
- Every programme roadmap must declare its status, target repository, evidence or exit
  criteria, non-goals, and relationship to other active programmes.
- A programme roadmap may refine implementation work but must not silently weaken
  `AGENTS.md`, architecture invariants, evaluation controls, approval gates, or
  source-write authority.
- When a programme completes, move it out of the active queue, summarize it in
  [`HISTORY.md`](HISTORY.md), and retain detailed records only when they remain useful as
  operator or architectural references.

## Documentation lifecycle

- Living required-reading documents are the README, root roadmap, architecture, pipeline,
  and conventions.
- Operator references remain focused on commands or subsystem contracts.
- Completed programme narratives are historical and must not become a second active
  roadmap.
- Prefer one historical index with links over multiple cross-referenced completion
  summaries in the required reading path.
