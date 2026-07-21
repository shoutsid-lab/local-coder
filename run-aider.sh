#!/usr/bin/env bash
set -euo pipefail

if ! curl -fsS http://127.0.0.1:8080/health >/dev/null; then
    echo "llama-server is not available on port 8080."
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "The repository has uncommitted changes."
    echo "Commit, stash, or discard them before starting a new task."
    exit 1
fi

aider \
    --model openai/local-coder \
    --edit-format whole \
    --read CONVENTIONS.md \
    --read TASK.md \
    "$@"
