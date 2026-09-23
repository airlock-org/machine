set -eu

repo="${AIRLOCK_CONFIG_REPO_PATH:-@NIX_0@}"
tmpdir="$(@NIX_1@/bin/mktemp -d)"
trap '@NIX_1@/bin/rm -rf "$tmpdir"' EXIT

current_tar="$tmpdir/current.tar"
@NIX_1@/bin/cat > "$current_tar"
@NIX_2@/bin/tar -tf "$current_tar" |
  @NIX_3@/bin/awk '
    $0 != "config.bundle" && $0 != "manifest.json" {
      printf "unexpected current config tar member: %s\n", $0 > "/dev/stderr"
      exit 1
    }
  '
@NIX_2@/bin/tar -C "$tmpdir" -xf "$current_tar"

[ -f "$tmpdir/config.bundle" ] || {
  printf 'current config tar is missing config.bundle\n' >&2
  exit 1
}
[ -f "$tmpdir/manifest.json" ] || {
  printf 'current config tar is missing manifest.json\n' >&2
  exit 1
}

manifest_schema="$(@NIX_4@/bin/jq -r '.schema' "$tmpdir/manifest.json")"
candidate_commit="$(@NIX_4@/bin/jq -r '.commit' "$tmpdir/manifest.json")"
branch="$(@NIX_4@/bin/jq -r '.branch // "main"' "$tmpdir/manifest.json")"
[ "$manifest_schema" = "airlock.config-candidate.v1" ] || {
  printf 'unsupported current config manifest schema: %s\n' "$manifest_schema" >&2
  exit 1
}
case "$candidate_commit" in
  ""|*[!0-9a-f]*)
    printf 'invalid current config commit: %s\n' "$candidate_commit" >&2
    exit 1
    ;;
esac
case "$branch" in
  ""|*[!A-Za-z0-9._/-]*)
    printf 'invalid current config branch: %s\n' "$branch" >&2
    exit 1
    ;;
esac

verify_repo="$tmpdir/verify.git"
@NIX_5@/bin/git init --bare -b main "$verify_repo" >/dev/null
@NIX_5@/bin/git -C "$verify_repo" bundle verify "$tmpdir/config.bundle" > "$tmpdir/bundle-verify.log" 2>&1
bundle_commit="$(
  @NIX_5@/bin/git -C "$verify_repo" bundle list-heads "$tmpdir/config.bundle" |
    @NIX_3@/bin/awk 'NR == 1 { print $1 }'
)"
[ "$bundle_commit" = "$candidate_commit" ] || {
  printf 'current config bundle commit %s does not match manifest commit %s\n' "$bundle_commit" "$candidate_commit" >&2
  exit 1
}

import_status=imported
reason="staging initialized from sys-config-vault current"
current_commit=""
dirty=""

clone_current_into_repo() {
  parent="${repo%/*}"
  @NIX_1@/bin/mkdir -p "$parent"
  clone_tmp="$tmpdir/staging-clone"
  @NIX_5@/bin/git clone "$tmpdir/config.bundle" "$clone_tmp" > "$tmpdir/git-clone.log" 2>&1
  @NIX_5@/bin/git -C "$clone_tmp" checkout -B "$branch" "$candidate_commit" > "$tmpdir/git-checkout.log" 2>&1
  cd "$tmpdir"
  @NIX_1@/bin/rm -rf "$repo"
  @NIX_1@/bin/mv "$clone_tmp" "$repo"
  current_commit="$candidate_commit"
}

if [ -d "$repo/.git" ]; then
  cd "$repo"
  dirty="$(@NIX_5@/bin/git status --porcelain)"
  if ! current_commit="$(@NIX_5@/bin/git rev-parse HEAD 2>/dev/null)"; then
    if [ -n "$dirty" ]; then
      import_status=skipped
      reason="staging empty Git repo is dirty; refusing to overwrite"
    else
      clone_current_into_repo
    fi
  elif [ "$current_commit" = "$candidate_commit" ] && [ -z "$dirty" ]; then
    import_status=current
    reason="staging already matches sys-config-vault current"
  elif [ -n "$dirty" ]; then
    import_status=skipped
    reason="staging worktree is dirty; refusing to overwrite"
  else
    import_status=skipped
    reason="staging already has a different committed worktree"
  fi
elif [ -e "$repo" ] && [ -n "$(@NIX_6@/bin/find "$repo" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
  import_status=skipped
  reason="staging path exists and is not an empty Git repository"
else
  clone_current_into_repo
fi

@NIX_4@/bin/jq -c -n \
  --arg schema "airlock.config-staging-import-current.v1" \
  --arg status "passed" \
  --arg import_status "$import_status" \
  --arg reason "$reason" \
  --arg repo "$repo" \
  --arg current_commit "$current_commit" \
  --arg candidate_commit "$candidate_commit" \
  --arg dirty "$dirty" \
  '{
    schema: $schema,
    status: $status,
    import_status: $import_status,
    reason: $reason,
    repo: $repo,
    current_commit: $current_commit,
    candidate_commit: $candidate_commit,
    dirty: $dirty
  }'
