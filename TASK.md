# Active Task

No implementation task is currently active.

For a role-separated run, pass the task directly:

```bash
./local-coder.py run "Describe one concrete coding task and its acceptance criteria"
```

The orchestrator writes that task into an ignored per-run task file inside the isolated
worktree. For the legacy planner or direct interactive Aider workflow, replace this file
with the current task before starting.
