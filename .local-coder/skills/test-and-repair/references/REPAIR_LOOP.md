# Bounded repair loop

For each repair iteration:

1. capture one deterministic failure and its exact evidence;
2. identify the narrowest root cause supported by that evidence;
3. issue one bounded edit against approved files;
4. rerun the failing check before broader verification;
5. inspect the diff for scope growth or regressions; and
6. stop or roll back when the next change would weaken a contract or broaden the task.

Do not repair multiple unrelated failures in one edit.
