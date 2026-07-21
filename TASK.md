# Goal

Add a unified command-line interface named `local-coder.py` for the existing local AI coding pipeline.

# Requirements

The command must support these subcommands:

* `status`

  * Check that llama-server is healthy on port 8080.
  * Check that LiteLLM is available on port 4000.
  * Show the current Git branch.
  * Show whether the working tree is clean.

* `task FILE [FILE ...]`

  * Launch `run-aider.sh task` with the supplied editable files.

* `repair INSTRUCTION FILE [FILE ...]`

  * Launch `run-aider.sh repair` with the supplied atomic instruction and editable files.

* `plan`

  * Run `create-plan.py`.
  * Display the generated `PLAN.candidate.json`.

* `execute`

  * Run `run-plan.py` using the approved `PLAN.json`.

* `verify`

  * Run `make verify`.

* `review`

  * Run `review-diff.py`.

# Constraints

* Use only the Python standard library.
* Reuse the existing scripts rather than duplicating their logic.
* Return the underlying command's exit code.
* Print commands before executing them.
* Do not commit automatically.
* Do not modify existing calculator behaviour.
* Do not modify protected contract tests.
* Keep the implementation in one new file: `local-coder.py`.

# Validation

Run:

```bash
.venv/bin/python -m py_compile local-coder.py
./local-coder.py status
./local-coder.py verify
make verify
```

