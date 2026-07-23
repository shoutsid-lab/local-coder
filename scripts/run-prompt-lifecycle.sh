#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/run-prompt-lifecycle.sh \
    CAMPAIGN_ID BUILD_ID HOLDOUT_MANIFEST HOLDOUT_ORACLE ACTOR [--activate]

Runs or reuses the campaign's paired prompt evaluation, derives the only
scorecard-consistent decision, closes and audits the campaign, and activates
only when every promotion gate passed and --activate was supplied.
EOF
}

if [[ $# -lt 5 || $# -gt 6 ]]; then
    usage >&2
    exit 64
fi

campaign_id="$1"
build_id="$2"
holdout_manifest="$(realpath "$3")"
holdout_oracle="$(realpath "$4")"
actor="$5"
activate_flag="${6:-}"

if [[ -n "$activate_flag" && "$activate_flag" != "--activate" ]]; then
    usage >&2
    exit 64
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"

if [[ ! -x .venv/bin/python ]]; then
    printf 'Project virtual environment is missing: %s/.venv\n' "$root" >&2
    exit 1
fi
if [[ ! -f "$holdout_manifest" || ! -f "$holdout_oracle" ]]; then
    printf 'Holdout manifest or oracle is missing.\n' >&2
    exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$root/.local-coder/prompt-lifecycle-runs/${stamp}-${campaign_id}"
mkdir -p "$run_dir"

campaign_json="$run_dir/campaign-before.json"
./local-coder.py show-campaign "$campaign_id" | tee "$campaign_json" >/dev/null

jq -e --arg build "$build_id" '
    (.status == "active" or (.status | startswith("completed_")))
    and any(.candidate_builds[]; .id == $build and .status == "candidate_ready")
' "$campaign_json" >/dev/null

evaluation_id="$(jq -r '
    [.evaluations[] | select(.status == "completed")] | last | .id // empty
' "$campaign_json")"

evaluation_json="$run_dir/evaluation.json"
evaluation_err="$run_dir/evaluation.stderr"
if [[ -z "$evaluation_id" ]]; then
    set +e
    ./local-coder.py evaluate \
        --campaign-id "$campaign_id" \
        --build-id "$build_id" \
        --holdout-suite "$holdout_manifest" \
        --holdout-oracle "$holdout_oracle" \
        2> >(tee "$evaluation_err" >&2) \
        | tee "$evaluation_json"
    evaluation_status=${PIPESTATUS[0]}
    set -e
    if [[ "$evaluation_status" -ne 0 && "$evaluation_status" -ne 2 ]]; then
        printf 'Prompt evaluation failed with exit code %s.\n' \
            "$evaluation_status" >&2
        exit "$evaluation_status"
    fi
    jq empty "$evaluation_json"
    evaluation_id="$(jq -r '.evaluation_id' "$evaluation_json")"
else
    printf 'Reusing completed evaluation %s.\n' "$evaluation_id"
fi

rationale="Automated lifecycle decision derived from the frozen paired prompt scorecard."
finalize_args=(
    finalize-prompt-campaign "$campaign_id"
    --actor "$actor"
    --rationale "$rationale"
)
if [[ "$activate_flag" == "--activate" ]]; then
    finalize_args+=(--activate)
fi

final_json="$run_dir/finalization.json"
final_err="$run_dir/finalization.stderr"
set +e
./local-coder.py "${finalize_args[@]}" \
    2> >(tee "$final_err" >&2) \
    | tee "$final_json"
final_status=${PIPESTATUS[0]}
set -e
if [[ "$final_status" -ne 0 && "$final_status" -ne 2 ]]; then
    printf 'Prompt finalization failed with exit code %s.\n' \
        "$final_status" >&2
    exit "$final_status"
fi
jq empty "$final_json"

./local-coder.py show-campaign "$campaign_id" \
    | tee "$run_dir/campaign-after.json" >/dev/null
./local-coder.py audit-campaign "$campaign_id" \
    | tee "$run_dir/audit.json" >/dev/null
./local-coder.py show-active-prompts \
    | tee "$run_dir/active-prompts.json" >/dev/null

jq -n \
    --arg run_dir "$run_dir" \
    --arg campaign_id "$campaign_id" \
    --arg evaluation_id "$evaluation_id" \
    --slurpfile final "$final_json" \
    --slurpfile audit "$run_dir/audit.json" \
    --slurpfile active "$run_dir/active-prompts.json" \
    '{
        run_dir: $run_dir,
        campaign_id: $campaign_id,
        evaluation_id: $evaluation_id,
        decision: $final[0].decision,
        campaign_status: $final[0].status,
        audit_passed: $audit[0].passed,
        activation_status: $final[0].activation_status,
        activation: $final[0].activation,
        active_prompts: $active[0].active_prompts
    }' | tee "$run_dir/summary.json"

printf '\nLifecycle evidence: %s\n' "$run_dir"
