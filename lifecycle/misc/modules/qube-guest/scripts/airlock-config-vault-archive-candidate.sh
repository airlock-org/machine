set -eu

repo="${AIRLOCK_CONFIG_ARCHIVE_PATH:-@NIX_0@}"
tmpdir="$(@NIX_1@/bin/mktemp -d)"
trap '@NIX_1@/bin/rm -rf "$tmpdir"' EXIT

candidate_tar="$tmpdir/candidate.tar"
@NIX_1@/bin/cat > "$candidate_tar"
@NIX_2@/bin/tar -tf "$candidate_tar" |
  @NIX_3@/bin/awk '
    $0 != "config.bundle" && $0 != "validation.json" && $0 != "manifest.json" && $0 != "topology.json" && $0 != "system.json" {
      printf "unexpected candidate tar member: %s\n", $0 > "/dev/stderr"
      exit 1
    }
  '
@NIX_2@/bin/tar -C "$tmpdir" -xf "$candidate_tar"

[ -f "$tmpdir/config.bundle" ] || {
  printf 'candidate tar is missing config.bundle\n' >&2
  exit 1
}
[ -f "$tmpdir/validation.json" ] || {
  printf 'candidate tar is missing validation.json\n' >&2
  exit 1
}
[ -f "$tmpdir/manifest.json" ] || {
  printf 'candidate tar is missing manifest.json\n' >&2
  exit 1
}
[ -f "$tmpdir/topology.json" ] || {
  printf 'candidate tar is missing topology.json\n' >&2
  exit 1
}
[ -f "$tmpdir/system.json" ] || {
  printf 'candidate tar is missing system.json\n' >&2
  exit 1
}

manifest_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/manifest.json")"
manifest_commit="$(@NIX_4@/bin/jq -r '.commit' "$tmpdir/manifest.json")"
validation_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/validation.json")"
validation_status="$(@NIX_4@/bin/jq -r '.status' "$tmpdir/validation.json")"
validation_commit="$(@NIX_4@/bin/jq -r '.commit' "$tmpdir/validation.json")"
system_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/system.json")"
[ "$manifest_schema" = "airlock.config-candidate.v1" ] || {
  printf 'unsupported candidate manifest schema: %s\n' "$manifest_schema" >&2
  exit 1
}
[ "$validation_schema" = "airlock.config-validation.v1" ] || {
  printf 'unsupported validation schema: %s\n' "$validation_schema" >&2
  exit 1
}
[ "$system_schema" = "airlock.system.v1" ] || {
  printf 'unsupported system schema: %s\n' "$system_schema" >&2
  exit 1
}
[ "$validation_status" = passed ] || {
  printf 'validation status is not passed: %s\n' "$validation_status" >&2
  exit 1
}
[ "$manifest_commit" = "$validation_commit" ] || {
  printf 'manifest commit %s does not match validation commit %s\n' "$manifest_commit" "$validation_commit" >&2
  exit 1
}

verify_repo="$tmpdir/verify.git"
@NIX_5@/bin/git init --bare -b main "$verify_repo" >/dev/null
@NIX_5@/bin/git -C "$verify_repo" bundle verify "$tmpdir/config.bundle" > "$tmpdir/bundle-verify.log" 2>&1
candidate_commit="$(
  @NIX_5@/bin/git -C "$verify_repo" bundle list-heads "$tmpdir/config.bundle" |
    @NIX_3@/bin/awk 'NR == 1 { print $1 }'
)"
[ -n "$candidate_commit" ] || {
  printf 'candidate bundle did not expose a commit\n' >&2
  exit 1
}
[ "$candidate_commit" = "$validation_commit" ] || {
  printf 'candidate commit %s does not match validation commit %s\n' "$candidate_commit" "$validation_commit" >&2
  exit 1
}

snapshot_tmp="$tmpdir/snapshot"
@NIX_5@/bin/git clone "$tmpdir/config.bundle" "$snapshot_tmp" > "$tmpdir/snapshot-git-clone.log" 2>&1
@NIX_5@/bin/git -C "$snapshot_tmp" checkout --detach "$candidate_commit" > "$tmpdir/snapshot-git-checkout.log" 2>&1
@NIX_1@/bin/rm -rf "$snapshot_tmp/.git"

if [ ! -d "$repo/.git" ]; then
  @NIX_6@/bin/airlock-config-vault-init >/dev/null
fi

archive_dir="$repo/candidates/$candidate_commit"
snapshot_dir="$archive_dir/snapshot"
@NIX_1@/bin/mkdir -p "$archive_dir"
@NIX_1@/bin/cp "$tmpdir/config.bundle" "$archive_dir/config.bundle"
@NIX_1@/bin/cp "$tmpdir/manifest.json" "$archive_dir/manifest.json"
@NIX_1@/bin/cp "$tmpdir/validation.json" "$archive_dir/validation.json"
@NIX_1@/bin/cp "$tmpdir/topology.json" "$archive_dir/topology.json"
@NIX_1@/bin/cp "$tmpdir/system.json" "$archive_dir/system.json"
@NIX_1@/bin/cp "$tmpdir/bundle-verify.log" "$archive_dir/bundle-verify.log"
@NIX_1@/bin/cp "$tmpdir/snapshot-git-clone.log" "$archive_dir/snapshot-git-clone.log"
@NIX_1@/bin/cp "$tmpdir/snapshot-git-checkout.log" "$archive_dir/snapshot-git-checkout.log"
@NIX_1@/bin/rm -rf "$snapshot_dir"
@NIX_1@/bin/cp -a "$snapshot_tmp" "$snapshot_dir"
@NIX_4@/bin/jq -c -n \
  --arg schema "airlock.config-candidate-archive-record.v1" \
  --arg candidate_commit "$candidate_commit" \
  --arg manifest_schema "$manifest_schema" \
  --arg validation_schema "$validation_schema" \
  --arg system_schema "$system_schema" \
  --arg snapshot "$archive_dir/snapshot" \
  --arg topology "$archive_dir/topology.json" \
  --arg system "$archive_dir/system.json" \
  --arg source "dom0" \
  '{
    schema: $schema,
    candidate_commit: $candidate_commit,
    manifest_schema: $manifest_schema,
    validation_schema: $validation_schema,
    system_schema: $system_schema,
    snapshot: $snapshot,
    topology: $topology,
    system: $system,
    archived_by: $source
  }' > "$archive_dir/metadata.json"
printf '%s\n' "$candidate_commit" > "$repo/latest-candidate"

cd "$repo"
@NIX_5@/bin/git add "candidates/$candidate_commit" latest-candidate
archive_status=current
if ! @NIX_5@/bin/git diff --cached --quiet; then
  @NIX_5@/bin/git \
    -c user.name='Airlock dom0 archive' \
    -c user.email='airlock-dom0@localhost' \
    commit -m "archive candidate ${candidate_commit:0:12}" >/dev/null
  archive_status=archived
fi

vault_commit="$(@NIX_5@/bin/git rev-parse HEAD)"
@NIX_4@/bin/jq -c -n \
  --arg schema "airlock.config-vault-archive.v1" \
  --arg status "passed" \
  --arg archive_status "$archive_status" \
  --arg candidate_commit "$candidate_commit" \
  --arg vault_commit "$vault_commit" \
  --arg archive_repo "$repo" \
  '{
    schema: $schema,
    status: $status,
    archive_status: $archive_status,
    candidate_commit: $candidate_commit,
    vault_commit: $vault_commit,
    archive_repo: $archive_repo
  }'
