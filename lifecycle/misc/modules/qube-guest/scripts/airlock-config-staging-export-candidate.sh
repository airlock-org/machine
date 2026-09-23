set -eu

repo="${AIRLOCK_CONFIG_REPO_PATH:-@NIX_0@}"
tmpdir="$(@NIX_1@/bin/mktemp -d)"
trap '@NIX_1@/bin/rm -rf "$tmpdir"' EXIT

if [ ! -d "$repo/.git" ]; then
  printf 'missing Git repo at %s; run airlock-config-staging-init first\n' "$repo" >&2
  exit 1
fi

cd "$repo"
dirty="$(@NIX_2@/bin/git status --porcelain)"
if [ -n "$dirty" ]; then
  printf 'config staging worktree is dirty; commit changes before export\n' >&2
  printf '%s\n' "$dirty" >&2
  exit 1
fi

commit="$(@NIX_2@/bin/git rev-parse HEAD)"
branch="$(@NIX_2@/bin/git symbolic-ref --short HEAD)"
@NIX_2@/bin/git bundle create "$tmpdir/config.bundle" HEAD "refs/heads/$branch" >/dev/null
@NIX_3@/bin/jq -c -n \
  --arg schema "airlock.config-candidate.v1" \
  --arg commit "$commit" \
  --arg branch "$branch" \
  --arg source_role "sys-config-staging" \
  '{schema: $schema, commit: $commit, branch: $branch, source_role: $source_role}' \
  > "$tmpdir/manifest.json"

exec @NIX_4@/bin/tar -C "$tmpdir" -cf - config.bundle manifest.json
