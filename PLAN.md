# Task Plan

## Goal

Describe the requested behaviour or bug fix in one concise paragraph.

## Constraints

* Preserve existing public behaviour unless explicitly changed below.
* Do not refactor unrelated code.
* Do not add dependencies unless required.
* Do not modify protected files.
* Every implementation step must pass independent verification.

## Protected Files

* `test_pipeline_contract.py`
* `CONVENTIONS.md`
* `TASK.md`
* `PLAN.md`

## Atomic Steps

### Step 1

Status: pending

Objective:

Describe one small, independently verifiable change.

Editable files:

* `path/to/file.py`

Atomic instruction:

> Replace this text with one precise instruction that describes exactly one transformation.

Validation:

```bash
make verify
```

Expected diff:

```diff
- old behaviour
+ new behaviour
```

### Step 2

Status: pending

Objective:

Add the next step only when it can be performed and verified independently.

Editable files:

* `path/to/file.py`

Atomic instruction:

> State exactly what should change and what must remain unchanged.

Validation:

```bash
make verify
```

Expected result:

Describe the externally observable result.

## Final Validation

After all atomic steps pass:

```bash
make verify
git diff --check
git status --short
```

## Approval

* [ ] Plan reviewed
* [ ] Editable files approved
* [ ] Protected files confirmed
* [ ] Each step is atomic
* [ ] Validation commands are defined
* [ ] Execution approved

