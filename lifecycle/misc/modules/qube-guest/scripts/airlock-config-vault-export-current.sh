set -eu

repo="${AIRLOCK_CONFIG_ARCHIVE_PATH:-@NIX_0@}"
tmpdir="$(@NIX_1@/bin/mktemp -d)"
trap '@NIX_1@/bin/rm -rf "$tmpdir"' EXIT

@NIX_2@/bin/airlock-config-vault-init >/dev/null

cd "$repo"
candidate_commit=""
if [ -f latest-deployment ]; then
  deployment_id="$(@NIX_1@/bin/head -n 1 latest-deployment)"
  if [ -n "$deployment_id" ] && [ -f "deployments/$deployment_id/deployment.json" ]; then
    candidate_commit="$(@NIX_3@/bin/jq -r '.candidate_commit // ""' "deployments/$deployment_id/deployment.json")"
  fi
fi
if { [ -z "$candidate_commit" ] || [ "$candidate_commit" = null ]; } && [ -f latest-current ]; then
  candidate_commit="$(@NIX_1@/bin/head -n 1 latest-current)"
fi
if { [ -z "$candidate_commit" ] || [ "$candidate_commit" = null ]; } && [ -f latest-candidate ]; then
  candidate_commit="$(@NIX_1@/bin/head -n 1 latest-candidate)"
fi

case "$candidate_commit" in
  ""|*[!0-9a-f]*)
    printf 'invalid current config commit: %s\n' "$candidate_commit" >&2
    exit 1
    ;;
esac
[ -f "candidates/$candidate_commit/config.bundle" ] || {
  printf 'current config bundle is missing: %s\n' "$candidate_commit" >&2
  exit 1
}
[ -f "candidates/$candidate_commit/manifest.json" ] || {
  printf 'current config manifest is missing: %s\n' "$candidate_commit" >&2
  exit 1
}
@NIX_3@/bin/jq -e \
  --arg commit "$candidate_commit" \
  '.schema == "airlock.config-candidate.v1" and .commit == $commit' \
  "candidates/$candidate_commit/manifest.json" >/dev/null

@NIX_1@/bin/cp "candidates/$candidate_commit/config.bundle" "$tmpdir/config.bundle"
@NIX_1@/bin/cp "candidates/$candidate_commit/manifest.json" "$tmpdir/manifest.json"
exec @NIX_4@/bin/tar -C "$tmpdir" -cf - config.bundle manifest.json
