#!/usr/bin/env bash
# E12: probe which fine-grained PAT permissions each org call needs.
# Usage: E12_PAT=<token> ./e12-pat-probe.sh   (the token is never printed)
# Prints the HTTP status per call. Widen one toggle in the PAT UI, re-run.
set -u
org=pbkimdev
probe() {
  local method=$1 path=$2 body=${3:-}
  local code
  if [ -n "$body" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $E12_PAT" -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" -d "$body" "https://api.github.com$path")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer $E12_PAT" -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" "https://api.github.com$path")
  fi
  printf '%-6s %-60s %s\n' "$method" "$path" "$code"
}
probe GET   "/orgs/$org/actions/runners"
probe GET   "/orgs/$org/actions/variables"
probe PATCH "/orgs/$org/actions/variables/RUNTIME_STATE" '{"value":"e12-probe"}'
probe POST  "/orgs/$org/actions/runners/generate-jitconfig" '{"name":"e12-probe","runner_group_id":1,"labels":["e12-probe"]}'
probe GET   "/organizations/$org/settings/billing/usage"
