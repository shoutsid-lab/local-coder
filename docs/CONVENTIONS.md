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
