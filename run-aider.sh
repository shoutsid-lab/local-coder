#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./run-aider.sh task FILE [FILE ...]
  ./run-aider.sh repair "ATOMIC INSTRUCTION" FILE [FILE ...]
EOF
}

mode="${1:-}"

case "$mode" in
    task)
        shift
        ;;

    repair)
        shift

        if [[ $# -lt 2 ]]; then
            usage
            exit 2
        fi

        instruction="$1"
        shift
        ;;

    *)
        usage
        exit 2
        ;;
esac

editable_files=("$@")

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

if ! (echo > /dev/tcp/127.0.0.1/4000) 2>/dev/null; then
    echo "LiteLLM is not listening on port 4000."
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "The repository has uncommitted changes."
    echo "Commit, stash, or discard them before starting."
    exit 1
fi

common_args=(
    --no-gitignore
    --model openai/local-fast
    --edit-format whole
    --read CONVENTIONS.md
    --read TASK.md
)

if [[ "$mode" == "task" ]]; then
    exec aider \
        "${common_args[@]}" \
        "${editable_files[@]}"
fi

failure_log="$(mktemp)"

cleanup() {
    rm -f "$failure_log"
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
    echo "Verification already passes. No repair is required."
    exit 0
fi

prompt=$(cat <<EOF
Apply exactly this atomic repair:

${instruction}

Rules:

- Modify only the editable files supplied to this invocation.
- Make no other changes.
- Preserve signatures, docstrings, exception classes, and behaviour unless
  the instruction explicitly changes them.
- Do not modify tests, TASK.md, CONVENTIONS.md, or protected files.
- Return a valid Aider whole-file edit.
EOF
)

echo
echo "Sending atomic repair to local-coder..."

aider \
    "${common_args[@]}" \
    --yes-always \
    --map-tokens 0 \
    --message "$prompt" \
    "${editable_files[@]}"

protected_files=(
    test_calculator.py
    test_pipeline_contract.py
    CONVENTIONS.md
    TASK.md
)

if ! git diff --quiet -- "${protected_files[@]}"; then
    echo
    echo "A protected file was modified."
    git diff -- "${protected_files[@]}"
    exit 1
fi

echo
echo "Running final independent verification..."

if ! make verify; then
    echo
    echo "Atomic repair failed verification."
    echo "Current diff:"
    git diff
    exit 1
fi

echo
echo "Atomic repair passed."
echo
echo "Review before committing:"
echo "  git diff"
echo "  git status --short"
