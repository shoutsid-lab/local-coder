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

## Roadmap conventions

- Keep root [`ROADMAP.md`](../ROADMAP.md) as the repository-wide active queue and index;
  do not replace it with a detailed programme plan.
- Put detailed active programme roadmaps under `docs/roadmaps/` and link them from the
  root roadmap and documentation index.
- Treat track letters as repository-global, monotonic identifiers. Tracks A–D are retired,
  the MCP control-plane roadmap owns Track E, the reasoning-capable model roadmap owns
  Track F, and the next separate programme starts at Track G. Never reuse a retired or
  allocated track label.
- Treat root `R1`, `R2`, and similar labels as queue identifiers, not programme-track
  labels.
- Every programme roadmap must declare its status, target repository, owned track label,
  constraints, phased exit criteria, non-goals, and succession rule.
- A programme roadmap may refine implementation work but must not silently weaken
  `AGENTS.md`, architecture invariants, evaluation controls, approval gates, or source-write
  authority.
- When a programme completes, mark its document complete, update `docs/HANDOFF.md` and
  `docs/VALIDATION_HISTORY.md`, and remove it from active work in the root roadmap.
