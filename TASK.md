# Goal

Repair the calculator implementation so division follows the documented contract.

# Editable files

- calculator.py

# Protected files

- test_calculator.py
- test_pipeline_contract.py
- CONVENTIONS.md
- TASK.md

# Acceptance criteria

- Both integer and floating-point inputs remain supported.
- Division returns a float.
- A zero dividend is valid and returns 0.0.
- A zero divisor raises ValueError with the required message.
- Type annotations and the exact docstring remain unchanged.
- Do not modify tests or protected files.
- `make verify` must pass.

# Validation

Run:

make verify
