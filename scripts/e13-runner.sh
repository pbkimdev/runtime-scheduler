#!/usr/bin/env bash
# E13: one throwaway ephemeral self-hosted runner, repo-scoped to the public
# lab, inside a Docker container (Arch is not a supported runner host; the
# container image is Ubuntu). Custom label only: lab-e13. It deregisters
# after one job because of EPHEMERAL=1.
# Registering a runner is an approval item; run only after Paul says yes.
set -euo pipefail
repo=pbkimdev/runtime-scheduler-lab
reg_token=$(gh api -X POST "repos/$repo/actions/runners/registration-token" --jq .token)
docker run --rm --name e13-runner \
  -e REPO_URL="https://github.com/$repo" \
  -e RUNNER_TOKEN="$reg_token" \
  -e RUNNER_NAME=e13-archbox-container \
  -e LABELS=lab-e13 \
  -e EPHEMERAL=1 \
  -e DISABLE_AUTO_UPDATE=1 \
  myoung34/github-runner:ubuntu-noble
