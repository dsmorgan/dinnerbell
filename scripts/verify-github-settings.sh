#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Read back every repository setting from the lockdown issue (#8, items 1-11) and compare it
# with the expected value. Prints a Markdown table and exits 1 if any row FAILs.
#
# Read-only: every request is a GET. Run it signed in to gh as a repository admin, because
# rulesets, Actions permissions and code scanning settings need admin read access.
#
# Usage: scripts/verify-github-settings.sh [OWNER/REPO]

set -uo pipefail

command -v gh >/dev/null || { echo "gh is required" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

repo=${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)} || exit 2
fails=0

# GET repos/$repo/$1. Prints the body, or nothing and returns 1 on an HTTP error.
get() {
  gh api --method GET "repos/$repo${1:+/$1}" 2>/dev/null
}

# Apply a jq filter to JSON, printing "error" when the JSON is missing or the filter fails.
q() {
  local out
  if [ -n "$1" ] && out=$(jq -r "$2" <<<"$1" 2>/dev/null); then
    echo "$out"
  else
    echo "error"
  fi
}

# row ITEM SETTING EXPECTED ACTUAL [OK]
# OK is "pass", "fail" or "na"; without it, the row passes when ACTUAL equals EXPECTED.
row() {
  local result=${5:-}
  if [ -z "$result" ]; then
    [ "$3" = "$4" ] && result=pass || result=fail
  fi
  case "$result" in
    pass) result=PASS ;;
    na) result="N/A" ;;
    *) result=FAIL; fails=$((fails + 1)) ;;
  esac
  printf '| %s | %s | %s | %s | %s |\n' "$1" "$2" "$3" "$4" "$result"
}

# subset EXPECTED ACTUAL: both comma-separated; succeeds if every expected item is in ACTUAL.
subset() {
  local item
  IFS=',' read -ra items <<<"$1"
  for item in "${items[@]}"; do
    item=${item# }
    [[ ",${2// /}," == *",$item,"* ]] || return 1
  done
}

pass_if() { "$@" && echo pass || echo fail; }

repo_json=$(get "") || { echo "Can't read $repo. Check gh auth status." >&2; exit 2; }
default_branch=$(q "$repo_json" .default_branch)

# Workflow state and whether it has ever succeeded.
workflow_rows() {
  local item=$1 file=$2 runs=${3:-} wf state count
  wf=$(get "actions/workflows/$file")
  state=$(q "$wf" '.state // "missing"')
  [ "$state" = error ] && state=missing
  row "$item" "\`$file\` workflow" active "$state"
  if [ -n "$runs" ]; then
    count=$(q "$(get "actions/workflows/$file/runs?status=success&branch=$default_branch&per_page=1")" '.total_count')
    if [[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -gt 0 ]; then
      row "$item" "\`$file\` has a green run on $default_branch" yes yes
    else
      row "$item" "\`$file\` has a green run on $default_branch" yes no
    fi
  fi
}

echo "Settings for $repo (default branch: $default_branch), read $(date -u +%Y-%m-%dT%H:%MZ)"
echo
echo "| # | Setting | Expected | Actual | Result |"
echo "|---|---|---|---|---|"

# 1. Private vulnerability reporting.
row 1 "Private vulnerability reporting" true "$(q "$(get private-vulnerability-reporting)" .enabled)"

# 2. Default branch ruleset. rules/branches aggregates every active ruleset that applies.
rules=$(get "rules/branches/$default_branch?per_page=100")
pr_rule=$(q "$rules" '[.[] | select(.type == "pull_request")] | if length == 0 then "missing" else "present" end')
row 2 "Require a pull request" present "$pr_rule"
row 2 "Required approvals" 0 "$(q "$rules" '[.[] | select(.type == "pull_request") | .parameters.required_approving_review_count] | if length == 0 then "missing" else (max | tostring) end')"
expected_checks="check, dco, dependency-review, image"
checks=$(q "$rules" '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context] | unique | if length == 0 then "none" else join(", ") end')
row 2 "Required status checks" "$expected_checks" "$checks" "$(pass_if subset "$expected_checks" "$checks")"
codeql=$(q "$rules" '
  if any(.[]; .type == "code_scanning" and any(.parameters.code_scanning_tools[]?; .tool == "CodeQL")) then "code_scanning rule"
  elif any(.[]; .type == "required_status_checks" and any(.parameters.required_status_checks[]?; .context == "CodeQL")) then "status check"
  else "missing" end')
row 2 "CodeQL required" "code_scanning rule or status check" "$codeql" "$([ "$codeql" = missing ] || [ "$codeql" = error ] && echo fail || echo pass)"
row 2 "Require branches to be up to date" false "$(q "$rules" '[.[] | select(.type == "required_status_checks") | .parameters.strict_required_status_checks_policy] | if length == 0 then "no checks rule" else (any | tostring) end')"
for rule in required_linear_history deletion non_fast_forward; do
  row 2 "Rule \`$rule\`" present "$(q "$rules" "if any(.[]; .type == \"$rule\") then \"present\" else \"missing\" end")"
done
bypass="none"
for id in $(q "$rules" '[.[].ruleset_id] | unique | .[]'); do
  [ "$id" = error ] && { bypass=error; break; }
  b=$(q "$(get "rulesets/$id")" '[.bypass_actors[] | if .actor_type == "RepositoryRole" and .actor_id == 5 then "admin (\(.bypass_mode))" else "\(.actor_type) \(.actor_id) (\(.bypass_mode))" end] | join(", ")')
  [ -n "$b" ] && bypass=$([ "$bypass" = none ] && echo "$b" || echo "$bypass; $b")
done
row 2 "Bypass actors" "admin (pull_request)" "$bypass"

# 3. Tag ruleset and immutable releases.
tag_rulesets=$(get "rulesets?targets=tag&per_page=100")
tag_id=$(q "$tag_rulesets" '[.[] | select(.target == "tag")] | .[].id' | while read -r id; do
  [ "$id" = error ] && break
  get "rulesets/$id" | jq -r 'select(.enforcement == "active" and (.conditions.ref_name.include | index("refs/tags/v*"))) | .id'
done | head -n 1)
if [ -n "$tag_id" ]; then
  tag=$(get "rulesets/$tag_id")
  row 3 "Tag ruleset on \`refs/tags/v*\`" active active
  tag_rules=$(q "$tag" '[.rules[].type] | sort | join(", ")')
  row 3 "Tag rules" "creation, deletion, update" "$tag_rules" "$(pass_if subset "creation, deletion, update" "$tag_rules")"
  row 3 "Tag bypass actors" "admin (always)" "$(q "$tag" '[.bypass_actors[] | if .actor_type == "RepositoryRole" and .actor_id == 5 then "admin (\(.bypass_mode))" else "\(.actor_type) \(.actor_id) (\(.bypass_mode))" end] | if length == 0 then "none" else join(", ") end')"
else
  row 3 "Tag ruleset on \`refs/tags/v*\`" active missing
fi
row 3 "Immutable releases" true "$(q "$(get immutable-releases)" .enabled)"

# 4. CodeQL default setup.
setup=$(get code-scanning/default-setup)
row 4 "CodeQL default setup" configured "$(q "$setup" .state)"
languages=$(q "$setup" '.languages | sort | join(", ")')
row 4 "CodeQL languages" "actions, javascript-typescript, python" "$languages" \
  "$(pass_if subset "actions, javascript-typescript, python" "$languages")"
htmx=$(q "$(get 'code-scanning/alerts?state=open&per_page=100')" '[.[] | select(.most_recent_instance.location.path | test("htmx"))] | length')
[ "$htmx" = error ] && htmx="no analysis"
row 4 "Open CodeQL alerts on vendored htmx" 0 "$htmx"

# 5, 6, 8. Workflows from this issue.
workflow_rows 5 scan-release.yml runs
workflow_rows 6 dependency-review.yml
actions=$(get actions/permissions)
selected=$(get actions/permissions/selected-actions)
row 7 "Allowed actions" selected "$(q "$actions" .allowed_actions)"
row 7 "SHA pinning required" true "$(q "$actions" .sha_pinning_required)"
row 7 "GitHub-owned actions allowed" true "$(q "$selected" .github_owned_allowed)"
row 7 "Verified-creator actions allowed" false "$(q "$selected" .verified_allowed)"
row 7 "Allowed action patterns" "anchore/*, astral-sh/*, docker/*, ossf/*" "$(q "$selected" '.patterns_allowed | sort | join(", ")')"
row 7 "Fork PR approval" all_external_contributors "$(q "$(get actions/permissions/fork-pr-contributor-approval)" .approval_policy)"
workflow_rows 8 scorecard.yml runs

# 9. Dependabot.
if get contents/.github/dependabot.yml >/dev/null; then dependabot=present; else dependabot=missing; fi
row 9 "\`.github/dependabot.yml\`" present "$dependabot"
if get vulnerability-alerts >/dev/null; then alerts=true; else alerts=false; fi
row 9 "Dependabot alerts" true "$alerts"
row 9 "Dependabot security updates" true "$(q "$(get automated-security-fixes)" .enabled)"
row 9 "Auto-merge allowed" false "$(q "$repo_json" .allow_auto_merge)"
workflow_rows 9 pip-audit.yml

# 10. Secret scanning. Non-provider patterns and validity checks need GitHub Secret Protection,
# which a personal public repository can't buy, so a disabled value there is N/A, not FAIL.
row 10 "Secret scanning" enabled "$(q "$repo_json" '.security_and_analysis.secret_scanning.status')"
row 10 "Push protection" enabled "$(q "$repo_json" '.security_and_analysis.secret_scanning_push_protection.status')"
protection=$(q "$repo_json" '.security_and_analysis.secret_protection.status // .security_and_analysis.advanced_security.status // "unavailable"')
for extra in non_provider_patterns validity_checks; do
  status=$(q "$repo_json" ".security_and_analysis.secret_scanning_$extra.status")
  if [ "$status" = enabled ]; then
    row 10 "Secret scanning: ${extra//_/ }" enabled enabled
  elif [ "$protection" != enabled ]; then
    row 10 "Secret scanning: ${extra//_/ }" enabled "$status (needs Secret Protection)" na
  else
    row 10 "Secret scanning: ${extra//_/ }" enabled "$status"
  fi
done

# 11. Merge settings and DCO.
row 11 "Delete branch on merge" true "$(q "$repo_json" .delete_branch_on_merge)"
row 11 "Allow merge commits" false "$(q "$repo_json" .allow_merge_commit)"
row 11 "Allow squash merges" true "$(q "$repo_json" .allow_squash_merge)"
row 11 "Allow rebase merges" true "$(q "$repo_json" .allow_rebase_merge)"
row 11 "Web commit sign-off required" true "$(q "$repo_json" .web_commit_signoff_required)"
workflow_rows 11 dco.yml

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails setting(s) FAIL."
  exit 1
fi
echo "Every setting passes."
