# Portable plan format

Produce an ordered list of atomic steps. Each step should contain:

1. a single observable objective;
2. one or two editable file paths;
3. one explicit transformation;
4. the focused verification that proves the step is complete; and
5. any dependency on an earlier step.

Do not combine discovery, implementation, repair, and review into one step. Keep protected
or generated files out of the editable scope.
