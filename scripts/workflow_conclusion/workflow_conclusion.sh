#!/usr/bin/env bash
#
# Resolve a workflow run's conclusion from its completed jobs. This is a
# drop-in replacement for technote-space/workflow-conclusion-action v3.0.3.

set -euo pipefail

# Least to most severe.
CONCLUSIONS=(neutral skipped success cancelled timed_out action_required failure)

die() {
    echo "Error: $*" >&2
    exit 1
}

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        true | 1 | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

repository="${GITHUB_REPOSITORY:-}"
run_id="${TARGET_RUN_ID:-${GITHUB_RUN_ID:-}}"
set_env_name="${SET_ENV_NAME-WORKFLOW_CONCLUSION}"
fallback="${FALLBACK_CONCLUSION:-skipped}"
strict_success="${STRICT_SUCCESS:-false}"

command -v gh >/dev/null 2>&1 || die "gh CLI not found on PATH."
[[ "$repository" == */* ]] || die "GITHUB_REPOSITORY must be set to 'owner/repo'."
[[ "$run_id" =~ ^[0-9]+$ ]] || die "TARGET_RUN_ID (or GITHUB_RUN_ID) must be a numeric run id."
[[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]] || die "GH_TOKEN (or GITHUB_TOKEN) must be set."

raw_conclusions="$(
    gh api --paginate \
        "/repos/${repository}/actions/runs/${run_id}/jobs" \
        --jq '.jobs[].conclusion | select(. != null)'
)" || die "gh api request failed for run ${run_id}."

conclusions=()
while IFS= read -r line; do
    [[ -n "$line" ]] && conclusions+=("$line")
done <<<"$raw_conclusions"

resolve_conclusion() {
    if [[ ${#conclusions[@]} -eq 0 ]]; then
        printf '%s' "$fallback"
        return
    fi

    if is_true "$strict_success"; then
        local conclusion
        for conclusion in "${conclusions[@]}"; do
            if [[ "$conclusion" != "success" ]]; then
                printf 'failure'
                return
            fi
        done
        printf 'success'
        return
    fi

    local rank present best=""
    for rank in "${CONCLUSIONS[@]}"; do
        for present in "${conclusions[@]}"; do
            [[ "$present" == "$rank" ]] && best="$rank"
        done
    done
    printf '%s' "${best:-$fallback}"
}

conclusion="$(resolve_conclusion)"

echo "::group::Conclusions"
printf '  %s\n' "${conclusions[@]+"${conclusions[@]}"}"
echo "::endgroup::"
echo "::group::Conclusion"
echo "  ${conclusion}"
echo "::endgroup::"

[[ -n "${GITHUB_OUTPUT:-}" ]] && echo "conclusion=${conclusion}" >>"$GITHUB_OUTPUT"
if [[ -n "$set_env_name" && -n "${GITHUB_ENV:-}" ]]; then
    echo "${set_env_name}=${conclusion}" >>"$GITHUB_ENV"
fi

echo "$conclusion"
