set -eu

repo="${AIRLOCK_CONFIG_REPO_PATH:-@NIX_0@}"
validation_dir="${AIRLOCK_CONFIG_VALIDATION_DIR:-@NIX_1@}"
tmpdir="$(@NIX_2@/bin/mktemp -d)"
trap '@NIX_2@/bin/rm -rf "$tmpdir"' EXIT

if [ ! -d "$repo/.git" ]; then
  printf 'missing Git repo at %s; run airlock-config-staging-init first\n' "$repo" >&2
  exit 1
fi

mkdir -p "$validation_dir/logs"
cd "$repo"

commit="$(@NIX_3@/bin/git rev-parse HEAD)"
result_file="$validation_dir/$commit.json"
result_tmp="$result_file.tmp"
topology_json="$validation_dir/topology-$commit.json"
system_json="$validation_dir/system-$commit.json"
staged_tar="$tmpdir/config-candidate-staged.tar"
validated_tar="$validation_dir/validated-candidate-$commit.tar"
validator_stderr="$validation_dir/logs/staging-validator-$commit.stderr.log"
dirty="$(@NIX_3@/bin/git status --porcelain)"

write_dirty_validation() {
  status="$1"
  reason="$2"

  @NIX_4@/bin/jq -c -n \
    --arg schema "airlock.config-validation.v1" \
    --arg status "$status" \
    --arg reason "$reason" \
    --arg validator_role "config-staging-local" \
    --arg repo "$repo" \
    --arg commit "$commit" \
    --arg dirty "$dirty" \
    '{
      schema: $schema,
      status: $status,
      reason: $reason,
      validator_role: $validator_role,
      repo: $repo,
      commit: $commit,
      checks: [
        {
          name: "git-clean",
          status: (if $dirty == "" then "passed" else "failed" end),
          details: $dirty
        },
        {
          name: "nix-eval-airlock-topology",
          status: "skipped",
          attr: "airlock.topology"
        },
        {
          name: "nix-eval-airlock-system",
          status: "skipped",
          attr: "airlock.system"
        },
        {
          name: "nix-flake-check-no-build",
          status: "skipped"
        }
      ]
    }' > "$result_tmp"
  mv "$result_tmp" "$result_file"
  cat "$result_file"
}

if [ -n "$dirty" ]; then
  write_dirty_validation failed "config staging worktree is dirty; commit changes before export"
  exit 1
fi

if ! @NIX_5@/bin/airlock-config-staging-export-candidate > "$staged_tar" 2> "$validator_stderr"; then
  cat "$validator_stderr" >&2 || true
  write_dirty_validation failed "config staging export failed"
  exit 1
fi

if ! AIRLOCK_CONFIG_VALIDATION_DIR="$validation_dir" \
  @NIX_6@/bin/airlock-config-validator-validate-candidate \
    < "$staged_tar" \
    > "$validated_tar" \
    2> "$validator_stderr"; then
  cat "$validator_stderr" >&2 || true
  if [ -r "$result_file" ]; then
    cat "$result_file"
  fi
  exit 1
fi

@NIX_7@/bin/tar -tf "$validated_tar" |
  @NIX_8@/bin/awk '
    $0 != "config.bundle" && $0 != "validation.json" && $0 != "manifest.json" && $0 != "topology.json" && $0 != "system.json" {
      printf "unexpected validated candidate tar member: %s\n", $0 > "/dev/stderr"
      exit 1
    }
  '
@NIX_7@/bin/tar -C "$tmpdir" -xf "$validated_tar" validation.json topology.json system.json
@NIX_2@/bin/cp "$tmpdir/topology.json" "$topology_json"
@NIX_2@/bin/cp "$tmpdir/system.json" "$system_json"

@NIX_4@/bin/jq -c \
  --arg repo "$repo" \
  --arg topology_json "$topology_json" \
  --arg system_json "$system_json" \
  --arg validated_tar "$validated_tar" \
  --arg validator_stderr "$validator_stderr" \
  '
    .repo = $repo
    | .validated_candidate = $validated_tar
    | .validator_stderr = $validator_stderr
    | .checks = (
        .checks
        | map(
            if .name == "nix-eval-airlock-topology" then
              .output = $topology_json
            elif .name == "airlock-topology-json" then
              .path = $topology_json
            elif .name == "nix-eval-airlock-system" then
              .output = $system_json
            elif .name == "airlock-system-json" then
              .path = $system_json
            else
              .
            end
          )
      )
  ' "$tmpdir/validation.json" > "$result_tmp"
mv "$result_tmp" "$result_file"
cat "$result_file"
@NIX_4@/bin/jq -e '.status == "passed"' "$result_file" >/dev/null
