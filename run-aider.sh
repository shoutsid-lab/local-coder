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

args=(
    --no-gitignore
    --model openai/local-coder
    --edit-format whole
    --read CONVENTIONS.md
    --read TASK.md
)

if [[ "$mode" == "repair" ]]; then
    args+=(--test)
fi

exec aider "${args[@]}" "$@"
