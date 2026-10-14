#!/usr/bin/env bash
set -euo pipefail

repo="$1"
# Standalone guest systems can start before a checkout has been imported.
[ -e "$repo" ] || exit 0
git -C "$repo" rev-parse --git-dir >/dev/null

for key in user.name user.email; do
  # Read the effective identity, including global and included configuration.
  # A malformed configuration is an error, not permission to replace it.
  if value="$(git -C "$repo" config --get "$key")"; then
    [ -z "$value" ] || continue
  else
    git_status=$?
    [ "$git_status" -eq 1 ] || exit "$git_status"
  fi
  case "$key" in
    user.name) value='Airlock User' ;;
    user.email) value='user@airlock.invalid' ;;
  esac
  git -C "$repo" config --local "$key" "$value"
done
