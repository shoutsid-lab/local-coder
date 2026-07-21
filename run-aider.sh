#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  ./run-aider.sh task FILE [FILE ...]
  ./run-aider.sh apply "ATOMIC INSTRUCTION" FILE [FILE ...]
  ./run-aider.sh repair "ATOMIC INSTRUCTION" FILE [FILE ...]
USAGE
}

mode="${1:-}"
instruction=""

case "$mode" in
    task)
        shift
        ;;
    apply|repair)
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
task_file="${LOCAL_CODER_TASK_FILE:-TASK.md}"

required_files=(
    CONVENTIONS.md
    "$task_file"
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

# Interactive tasks and standalone repairs must begin from a clean repository.
# Agentic apply mode intentionally supports an existing worktree diff so a plan
# can be implemented through several atomic edits.
if [[ "$mode" != "apply" && -n "$(git status --porcelain)" ]]; then
    echo "The repository has uncommitted changes."
    echo "Commit, stash, or discard them before starting."
    exit 1
fi

mapfile -t contract_files < <(git ls-files '*_contract.py')
pipeline_controls=(
    run-aider.sh
    run-plan.py
    create-plan.py
    review-diff.py
    local-coder.py
    Makefile
    litellm-config.yaml
)
protected_files=(
    CONVENTIONS.md
    TASK.md
    "$task_file"
    "${contract_files[@]}"
    "${pipeline_controls[@]}"
)

for editable_file in "${editable_files[@]}"; do
    normalized_file="${editable_file#./}"
    if [[ "$normalized_file" == *_contract.py ]]; then
        echo "Protected contract file cannot be edited: $editable_file"
        exit 1
    fi
    for protected_file in "${protected_files[@]}"; do
        [[ -z "$protected_file" ]] && continue
        if [[ "$normalized_file" == "${protected_file#./}" ]]; then
            echo "Protected file cannot be edited: $editable_file"
            exit 1
        fi
    done
done

common_args=(
    --no-gitignore
    --model openai/local-fast
    --edit-format whole
    --read CONVENTIONS.md
    --read "$task_file"
)

if [[ "$mode" == "task" ]]; then
    exec aider \
        "${common_args[@]}" \
        "${editable_files[@]}"
fi

if [[ "$mode" == "repair" ]]; then
    echo "Running independent verification..."
    failure_log="$(mktemp)"
    trap 'rm -f "$failure_log"' EXIT

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
fi

prompt=$(cat <<PROMPT
Apply exactly this atomic change:

${instruction}

Rules:

- Modify only the editable files supplied to this invocation.
- Make no other changes.
- Preserve signatures, docstrings, exception classes, and behavior unless the
  instruction explicitly changes them.
- Never modify contract tests, ${task_file}, CONVENTIONS.md, or pipeline controls.
- Return a valid Aider whole-file edit.
PROMPT
)

echo
echo "Sending atomic change to local-fast..."

aider \
    "${common_args[@]}" \
    --yes-always \
    --map-tokens 0 \
    --message "$prompt" \
    "${editable_files[@]}"

if ! git diff --quiet -- "${protected_files[@]}"; then
    echo
    echo "A protected file was modified."
    git diff -- "${protected_files[@]}"
    exit 1
fi

if ! git diff --check; then
    echo
    echo "The atomic change introduced invalid whitespace or conflict markers."
    exit 1
fi

if [[ "$mode" == "apply" ]]; then
    echo
    echo "Atomic change applied. Full verification is deferred to the orchestrator."
    echo "Current status:"
    git status --short
    exit 0
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
