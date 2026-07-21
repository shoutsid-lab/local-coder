#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"

case "$mode" in
    task|repair)
        shift
        ;;
    *)
        echo "Usage: $0 {task|repair} [editable files...]"
        exit 2
        ;;
esac

required_files=(
    CONVENTIONS.md
    TASK.md
    test_calculator.py
    test_pipeline_contract.py
    .venv/bin/python
)

for required_file in "${required_files[@]}"; do
    if [[ ! -e "$required_file" ]]; then
        echo "Required file is missing: $required_file"
        exit 1
    fi
done

if ! curl -fsS http://127.0.0.1:8080/health >/dev/null; then
    echo "llama-server is not healthy on port 8080."
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "The repository has uncommitted changes."
    echo "Commit, stash, or discard them before starting."
    exit 1
fi

common_args=(
    --no-gitignore
    --yes-always
    --model openai/local-coder
    --edit-format whole
    --read CONVENTIONS.md
    --read TASK.md
)

if [[ "$mode" == "task" ]]; then
    exec aider "${common_args[@]}" "$@"
fi

max_attempts="${MAX_REPAIR_ATTEMPTS:-3}"
failure_log="$(mktemp)"
message_file="$(mktemp)"

cleanup() {
    rm -f "$failure_log" "$message_file"
}
trap cleanup EXIT

protected_files=(
    test_calculator.py
    test_pipeline_contract.py
    CONVENTIONS.md
    TASK.md
)

for attempt in $(seq 1 "$max_attempts"); do
    echo
    echo "Verification attempt $attempt of $max_attempts..."

    set +e
    make verify >"$failure_log" 2>&1
    verify_status=$?
    set -e

    cat "$failure_log"

    if [[ $verify_status -eq 0 ]]; then
        echo
        echo "Repair passed independent verification."
        echo "Review the uncommitted changes:"
        echo "  git diff"
        echo "  git status --short"
        exit 0
    fi

    {
        cat <<'EOF'
Repair the failing implementation using the smallest possible change.

The contract below is non-negotiable:

- Keep this exact signature:
  def divide(a: int | float, b: int | float) -> float:
- Keep this exact docstring:
  """Return a divided by b, raising ValueError when b is zero."""
- A zero dividend is valid.
- When b == 0, raise ValueError("Cannot divide by zero").
- Do not use ZeroDivisionError.
- Do not alter tests, conventions, task requirements, or protected files.
- Modify only the editable files supplied to this invocation.
- Return a valid Aider whole-file edit.
- Do not claim the contract passes unless your implementation matches it exactly.

Protected test source follows. Read it, but do not edit it.

--- test_calculator.py ---
EOF

        cat test_calculator.py

        cat <<'EOF'

--- test_pipeline_contract.py ---
EOF

        cat test_pipeline_contract.py

        cat <<'EOF'

--- independent verification output ---
EOF

        tail -n 250 "$failure_log"
    } >"$message_file"

    echo
    echo "Sending repair attempt $attempt to local-coder..."

    set +e
    aider \
        "${common_args[@]}" \
        --message-file "$message_file" \
        "$@"
    aider_status=$?
    set -e

    if ! git diff --quiet -- "${protected_files[@]}"; then
        echo
        echo "A protected file was modified. Aborting."
        git diff -- "${protected_files[@]}"
        exit 1
    fi

    if [[ $aider_status -ne 0 ]]; then
        echo "Aider returned status $aider_status; continuing to verification."
    fi
done

echo
echo "Final independent verification..."

if make verify; then
    echo
    echo "Repair passed independent verification."
    exit 0
fi

echo
echo "Repair failed after $max_attempts fresh-context attempts."
echo "Current uncommitted diff:"
git diff
exit 1
