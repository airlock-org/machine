    set -eu

    repo="${AIRLOCK_CONFIG_ARCHIVE_PATH:-@NIX_0@}"
    config_inputs_root="${AIRLOCK_CONFIG_INPUTS_PATH:-@NIX_1@}"
    tmpdir="$(@NIX_2@/bin/mktemp -d)"
    trap '@NIX_2@/bin/rm -rf "$tmpdir"' EXIT

    @NIX_3@
    @NIX_4@

    config_inputs_ready() {
      [ -d "$config_inputs_root/nixpkgs" ] &&
        [ -d "$config_inputs_root/nixpkgs-libvirt_10_5" ] &&
        [ -d "$config_inputs_root/nixos-qubes" ] &&
        [ -d "$config_inputs_root/airlock-support" ]
    }

    write_archive_readme() {
      cat > README.md <<'EOF'
# Airlock Configuration Vault

This repository archives Airlock configuration baselines, candidates, and
deployment records written by dom0.

The `current` link points at the latest config snapshot that should be used
to initialize an editable sys-config-staging worktree.
EOF
    }

    commit_archive_if_needed() {
      message="$1"
      if ! @NIX_5@/bin/git diff --cached --quiet; then
        @NIX_5@/bin/git \
          -c user.name='Airlock Config Vault' \
          -c user.email='airlock-config-vault@localhost' \
          commit -m "$message"
      fi
    }

    ensure_archive_repo() {
      mkdir -p "$repo"
      cd "$repo"
      if [ ! -d .git ]; then
        @NIX_5@/bin/git init -b main
      fi
      mkdir -p candidates deployments
      : > candidates/.gitkeep
      : > deployments/.gitkeep
      write_archive_readme
      @NIX_5@/bin/git add README.md candidates/.gitkeep deployments/.gitkeep
    }

    set_current_link() {
      candidate_commit="$1"
      [ -d "candidates/$candidate_commit/snapshot" ] || {
        printf 'current candidate snapshot is missing: %s\n' "$candidate_commit" >&2
        exit 1
      }
      printf '%s\n' "$candidate_commit" > latest-current
      @NIX_2@/bin/ln -sfn "candidates/$candidate_commit/snapshot" current
      @NIX_5@/bin/git add latest-current current
    }

    set_current_from_latest_deployment() {
      [ -f latest-deployment ] || return 1
      deployment_id="$(@NIX_2@/bin/head -n 1 latest-deployment)"
      [ -n "$deployment_id" ] || return 1
      [ -f "deployments/$deployment_id/deployment.json" ] || return 1
      candidate_commit="$(@NIX_6@/bin/jq -r '.candidate_commit // ""' "deployments/$deployment_id/deployment.json")"
      [ -n "$candidate_commit" ] && [ "$candidate_commit" != null ] || return 1
      set_current_link "$candidate_commit"
      commit_archive_if_needed "record current config from deployment $deployment_id"
      return 0
    }

    create_initial_current() {
      initial_repo="$tmpdir/initial-config"
      @NIX_2@/bin/mkdir -p "$initial_repo"
      cd "$initial_repo"
      write_initial_runtime_config_repo_files
      substitute_config_inputs "$config_inputs_root"
      @NIX_5@/bin/git init -b main
      @NIX_5@/bin/git add @NIX_7@
      if config_inputs_ready; then
        @NIX_8@/bin/airlock-config-nix \
          --extra-experimental-features nix-command \
          --extra-experimental-features flakes \
          flake lock \
          --impure \
          --offline
      else
        printf 'config vault config inputs are not present yet under %s; flake.lock deferred\n' "$config_inputs_root"
      fi
      if [ -f flake.lock ]; then
        @NIX_5@/bin/git add flake.lock
      fi
      @NIX_5@/bin/git \
        -c user.name='Airlock Initial Runtime Config' \
        -c user.email='airlock-initial-runtime-config@localhost' \
        commit -m 'init airlock config candidate'

      candidate_commit="$(@NIX_5@/bin/git rev-parse HEAD)"
      archive_dir="$repo/candidates/$candidate_commit"
      snapshot_dir="$archive_dir/snapshot"
      @NIX_2@/bin/mkdir -p "$archive_dir" "$snapshot_dir"
      @NIX_5@/bin/git bundle create "$archive_dir/config.bundle" HEAD refs/heads/main >/dev/null
      @NIX_6@/bin/jq -c -n \
        --arg schema "airlock.config-candidate.v1" \
        --arg commit "$candidate_commit" \
        --arg branch "main" \
        --arg source_role "sys-config-vault" \
        --arg source "initial-runtime-config" \
        '{
          schema: $schema,
          commit: $commit,
          branch: $branch,
          source_role: $source_role,
          source: $source
        }' > "$archive_dir/manifest.json"
      @NIX_5@/bin/git archive --format=tar HEAD |
        @NIX_9@/bin/tar -C "$snapshot_dir" -xf -
      @NIX_6@/bin/jq -c -n \
        --arg schema "airlock.config-vault-current.v1" \
        --arg status "current" \
        --arg candidate_commit "$candidate_commit" \
        --arg branch "main" \
        --arg source "initial-runtime-config" \
        --arg snapshot "$archive_dir/snapshot" \
        --arg bundle "$archive_dir/config.bundle" \
        '{
          schema: $schema,
          status: $status,
          candidate_commit: $candidate_commit,
          branch: $branch,
          source: $source,
          snapshot: $snapshot,
          bundle: $bundle
        }' > "$archive_dir/metadata.json"

      cd "$repo"
      printf '%s\n' "$candidate_commit" > latest-candidate
      set_current_link "$candidate_commit"
      @NIX_5@/bin/git add "candidates/$candidate_commit" latest-candidate
      commit_archive_if_needed "initialize current config baseline ${candidate_commit:0:12}"
    }

    ensure_archive_repo
    if [ -f latest-current ]; then
      current_commit="$(@NIX_2@/bin/head -n 1 latest-current)"
      if [ -n "$current_commit" ] && [ -d "candidates/$current_commit/snapshot" ]; then
        set_current_link "$current_commit"
        commit_archive_if_needed "repair current config pointer"
        printf 'config archive current exists: %s %s\n' "$repo" "$current_commit"
        exit 0
      fi
    fi
    if set_current_from_latest_deployment; then
      printf 'config archive current restored from latest deployment: %s\n' "$repo"
      exit 0
    fi
    create_initial_current
    printf 'config archive current initialized: %s\n' "$repo"
