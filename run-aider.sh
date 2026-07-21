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
    --model openai/local-coder
    --edit-format whole
    --read CONVENTIONS.md
    --read TASK.md
)

if [[ "$mode" == "task" ]]; then
    exec aider "${common_args[@]}" "$@"
fi

failure_log="$(mktemp)"
message_file="$(mktemp)"

cleanup() {
    rm -f "$failure_log" "$message_file"
}
trap cleanup EXIT

echo "Running independent verification..."

set +e
make verify >"$failure_log" 2>&1
verify_status=$?
set -e

cat "$failure_log"

if [[ $verify_status -eq 0 ]]; then
    echo
    echo "Verification already passes. No repair is needed."
    exit 0
fi

{
    cat <<'EOF'
Read TASK.md and repair the failing implementation.

The output below comes from the independent verification pipeline.

Requirements:

- Diagnose the failure from the traceback rather than assuming the code is correct.
- Modify only the editable files supplied to this Aider invocation.
- Do not modify tests, TASK.md, CONVENTIONS.md, or protected files.
- Make the smallest change that satisfies the documented contract.
- Preserve existing type annotations and the exact docstring.
- After editing, run the configured verification command and repair any
  remaining failures.
- Output edits using Aider's required whole-file edit format.

Independent verification output:

EOF

    # Prevent unexpectedly huge logs from filling the model context.
    tail -n 300 "$failure_log"
} >"$message_file"

echo
echo "Sending the verified failure to local-coder..."

set +e
aider \
    "${common_args[@]}" \
    --yes-always \
    --message-file "$message_file" \
    "$@"
aider_status=$?
set -e

echo
echo "Running final independent verification..."

set +e
make verify
final_status=$?
set -e

if [[ $final_status -ne 0 ]]; then
    echo
    echo "Repair failed independent verification."
    echo "Aider exit status: $aider_status"
    exit 1
fi

echo
echo "Repair passed independent verification."
echo "Review the uncommitted diff before committing:"
echo
echo "  git diff"
echo "  git status --short"
