{
  cfg,
  lib,
  pkgs,
  roleGit,
  configNix,
  configInputsPublicPath,
  configInputPathSubstitutionScript,
  initialRuntimeConfigRepoFilesScript,
  initialRuntimeConfigRepoGitAddArgs,
  configValidatorValidateCandidate,
}:
rec {
  configStagingImportCurrent = pkgs.writeShellScriptBin "airlock-config-staging-import-current" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_REPO_PATH:-${cfg.configRepoPath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    current_tar="$tmpdir/current.tar"
    ${pkgs.coreutils}/bin/cat > "$current_tar"
    ${pkgs.gnutar}/bin/tar -tf "$current_tar" |
      ${pkgs.gawk}/bin/awk '
        $0 != "config.bundle" && $0 != "manifest.json" {
          printf "unexpected current config tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '
    ${pkgs.gnutar}/bin/tar -C "$tmpdir" -xf "$current_tar"

    [ -f "$tmpdir/config.bundle" ] || {
      printf 'current config tar is missing config.bundle\n' >&2
      exit 1
    }
    [ -f "$tmpdir/manifest.json" ] || {
      printf 'current config tar is missing manifest.json\n' >&2
      exit 1
    }

    manifest_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/manifest.json")"
    candidate_commit="$(${pkgs.jq}/bin/jq -r '.commit' "$tmpdir/manifest.json")"
    branch="$(${pkgs.jq}/bin/jq -r '.branch // "main"' "$tmpdir/manifest.json")"
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
    ${roleGit}/bin/git init --bare -b main "$verify_repo" >/dev/null
    ${roleGit}/bin/git -C "$verify_repo" bundle verify "$tmpdir/config.bundle" > "$tmpdir/bundle-verify.log" 2>&1
    bundle_commit="$(
      ${roleGit}/bin/git -C "$verify_repo" bundle list-heads "$tmpdir/config.bundle" |
        ${pkgs.gawk}/bin/awk 'NR == 1 { print $1 }'
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
      parent="''${repo%/*}"
      ${pkgs.coreutils}/bin/mkdir -p "$parent"
      clone_tmp="$tmpdir/staging-clone"
      ${roleGit}/bin/git clone "$tmpdir/config.bundle" "$clone_tmp" > "$tmpdir/git-clone.log" 2>&1
      ${roleGit}/bin/git -C "$clone_tmp" checkout -B "$branch" "$candidate_commit" > "$tmpdir/git-checkout.log" 2>&1
      cd "$tmpdir"
      ${pkgs.coreutils}/bin/rm -rf "$repo"
      ${pkgs.coreutils}/bin/mv "$clone_tmp" "$repo"
      current_commit="$candidate_commit"
    }

    if [ -d "$repo/.git" ]; then
      cd "$repo"
      dirty="$(${roleGit}/bin/git status --porcelain)"
      if ! current_commit="$(${roleGit}/bin/git rev-parse HEAD 2>/dev/null)"; then
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
    elif [ -e "$repo" ] && [ -n "$(${pkgs.findutils}/bin/find "$repo" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
      import_status=skipped
      reason="staging path exists and is not an empty Git repository"
    else
      clone_current_into_repo
    fi

    ${pkgs.jq}/bin/jq -c -n \
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
  '';

  configCurrentImportService = pkgs.writeShellScript "airlock-config-current-import-qrexec" ''
    exec ${configStagingImportCurrent}/bin/airlock-config-staging-import-current
  '';

  configStagingInit = pkgs.writeShellScriptBin "airlock-config-staging-init" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_REPO_PATH:-${cfg.configRepoPath}}"
    config_inputs_root="''${AIRLOCK_CONFIG_INPUTS_PATH:-${configInputsPublicPath}}"

    config_inputs_ready() {
      [ -d "$config_inputs_root/nixpkgs" ] &&
        [ -d "$config_inputs_root/nixpkgs-libvirt_10_5" ] &&
        [ -d "$config_inputs_root/nixos-qubes" ] &&
        [ -d "$config_inputs_root/airlock-support" ]
    }

    ${initialRuntimeConfigRepoFilesScript}
    ${configInputPathSubstitutionScript}

    config_inputs_need_refresh() {
      for input_file in flake.nix flake.lock; do
        [ -f "$input_file" ] || continue
        ${pkgs.gnugrep}/bin/grep -F '__AIRLOCK_NIXPKGS_URL__' "$input_file" >/dev/null 2>&1 && return 0
        ${pkgs.gnugrep}/bin/grep -F '/rw/airlock/config-inputs/' "$input_file" >/dev/null 2>&1 && return 0
        ${pkgs.gnugrep}/bin/grep -F '/rw/home/${lib.escapeShellArg cfg.user}/.local/share/airlock/config-inputs/' "$input_file" >/dev/null 2>&1 && return 0
        ${pkgs.gnugrep}/bin/grep -F '${cfg.configInputsPath}/' "$input_file" >/dev/null 2>&1 && return 0
      done
      return 1
    }

    require_clean_worktree_for_input_update() {
      dirty="$(${roleGit}/bin/git status --porcelain)"
      if [ -n "$dirty" ]; then
        printf 'config staging worktree is dirty; refusing to refresh config input paths\n%s\n' "$dirty" >&2
        exit 1
      fi
    }

    add_config_input_lock_if_ready() {
      if [ -f flake.lock ]; then
        printf 'config staging flake.lock already exists: %s\n' "$repo/flake.lock"
        return 0
      fi

      if ! config_inputs_ready; then
        printf 'config staging config inputs are not present yet under %s; flake.lock deferred\n' "$config_inputs_root"
        return 0
      fi

      substitute_config_inputs "$config_inputs_root"
      ${configNix}/bin/airlock-config-nix \
        --extra-experimental-features nix-command \
        --extra-experimental-features flakes \
        flake lock \
        --impure \
        --offline
      ${roleGit}/bin/git add flake.nix flake.lock
    }

    if [ -d "$repo/.git" ]; then
      cd "$repo"
      if [ "''${AIRLOCK_CONFIG_REFRESH_INPUTS:-0}" = 1 ]; then
        if ! config_inputs_ready; then
          printf 'config staging config inputs are not present yet under %s; refresh deferred\n' "$config_inputs_root"
          printf 'config staging repo already exists: %s\n' "$repo"
          exit 0
        fi
        dirty="$(${roleGit}/bin/git status --porcelain)"
        if [ -n "$dirty" ]; then
          printf 'config staging config input refresh deferred because worktree is dirty\n%s\n' "$dirty"
        else
          substitute_config_inputs "$config_inputs_root"
          ${configNix}/bin/airlock-config-nix \
            --extra-experimental-features nix-command \
            --extra-experimental-features flakes \
            flake lock \
            --recreate-lock-file \
            --impure \
            --offline
          if [ -n "$(${roleGit}/bin/git status --porcelain flake.nix flake.lock)" ]; then
            ${roleGit}/bin/git add flake.nix flake.lock
            ${roleGit}/bin/git \
              -c user.name='Airlock Config Staging' \
              -c user.email='airlock-config-staging@localhost' \
              commit -m 'refresh airlock config inputs'
          elif config_inputs_need_refresh; then
            printf 'config staging config input paths still need refresh after lock update\n' >&2
            exit 1
          else
            printf 'config staging config input lock is current\n'
          fi
        fi
      elif config_inputs_need_refresh; then
        printf 'config staging config input refresh is required but deferred for existing repo: %s\n' "$repo"
      fi
      if [ ! -f flake.lock ]; then
        require_clean_worktree_for_input_update
        add_config_input_lock_if_ready
        if ! ${roleGit}/bin/git diff --cached --quiet; then
          ${roleGit}/bin/git \
            -c user.name='Airlock Config Staging' \
            -c user.email='airlock-config-staging@localhost' \
            commit -m 'lock airlock config candidate inputs'
        fi
      fi
      printf 'config staging repo already exists: %s\n' "$repo"
      exit 0
    fi

    if [ "''${AIRLOCK_CONFIG_STAGING_INIT_FROM_INITIAL_CONFIG:-0}" != 1 ]; then
      ${pkgs.coreutils}/bin/mkdir -p "$repo"
      if [ -n "$(${pkgs.findutils}/bin/find "$repo" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
        printf 'config staging path exists but is not a Git repo; waiting for sys-config-vault current import: %s\n' "$repo"
      else
        printf 'config staging repo is empty; waiting for sys-config-vault current import: %s\n' "$repo"
      fi
      exit 0
    fi

    mkdir -p "$repo"
    cd "$repo"
    ${roleGit}/bin/git init -b main
    write_initial_runtime_config_repo_files
    substitute_config_inputs "$config_inputs_root"
    ${roleGit}/bin/git add ${initialRuntimeConfigRepoGitAddArgs}
    add_config_input_lock_if_ready
    ${roleGit}/bin/git \
      -c user.name='Airlock Config Staging' \
      -c user.email='airlock-config-staging@localhost' \
      commit -m 'init airlock config candidate'
  '';

  configStagingValidate = pkgs.writeShellScriptBin "airlock-config-staging-validate" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_REPO_PATH:-${cfg.configRepoPath}}"
    validation_dir="''${AIRLOCK_CONFIG_VALIDATION_DIR:-${cfg.configValidationPath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    if [ ! -d "$repo/.git" ]; then
      printf 'missing Git repo at %s; run airlock-config-staging-init first\n' "$repo" >&2
      exit 1
    fi

    mkdir -p "$validation_dir/logs"
    cd "$repo"

    commit="$(${roleGit}/bin/git rev-parse HEAD)"
    result_file="$validation_dir/$commit.json"
    result_tmp="$result_file.tmp"
    topology_json="$validation_dir/topology-$commit.json"
    system_json="$validation_dir/system-$commit.json"
    staged_tar="$tmpdir/config-candidate-staged.tar"
    validated_tar="$validation_dir/validated-candidate-$commit.tar"
    validator_stderr="$validation_dir/logs/staging-validator-$commit.stderr.log"
    dirty="$(${roleGit}/bin/git status --porcelain)"

    write_dirty_validation() {
      status="$1"
      reason="$2"

      ${pkgs.jq}/bin/jq -c -n \
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

    if ! ${configStagingExportCandidate}/bin/airlock-config-staging-export-candidate > "$staged_tar" 2> "$validator_stderr"; then
      cat "$validator_stderr" >&2 || true
      write_dirty_validation failed "config staging export failed"
      exit 1
    fi

    if ! AIRLOCK_CONFIG_VALIDATION_DIR="$validation_dir" \
      ${configValidatorValidateCandidate}/bin/airlock-config-validator-validate-candidate \
        < "$staged_tar" \
        > "$validated_tar" \
        2> "$validator_stderr"; then
      cat "$validator_stderr" >&2 || true
      if [ -r "$result_file" ]; then
        cat "$result_file"
      fi
      exit 1
    fi

    ${pkgs.gnutar}/bin/tar -tf "$validated_tar" |
      ${pkgs.gawk}/bin/awk '
        $0 != "config.bundle" && $0 != "validation.json" && $0 != "manifest.json" && $0 != "topology.json" && $0 != "system.json" {
          printf "unexpected validated candidate tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '
    ${pkgs.gnutar}/bin/tar -C "$tmpdir" -xf "$validated_tar" validation.json topology.json system.json
    ${pkgs.coreutils}/bin/cp "$tmpdir/topology.json" "$topology_json"
    ${pkgs.coreutils}/bin/cp "$tmpdir/system.json" "$system_json"

    ${pkgs.jq}/bin/jq -c \
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
    ${pkgs.jq}/bin/jq -e '.status == "passed"' "$result_file" >/dev/null
  '';

  configStagingExportCandidate = pkgs.writeShellScriptBin "airlock-config-staging-export-candidate" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_REPO_PATH:-${cfg.configRepoPath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    if [ ! -d "$repo/.git" ]; then
      printf 'missing Git repo at %s; run airlock-config-staging-init first\n' "$repo" >&2
      exit 1
    fi

    cd "$repo"
    dirty="$(${roleGit}/bin/git status --porcelain)"
    if [ -n "$dirty" ]; then
      printf 'config staging worktree is dirty; commit changes before export\n' >&2
      printf '%s\n' "$dirty" >&2
      exit 1
    fi

    commit="$(${roleGit}/bin/git rev-parse HEAD)"
    branch="$(${roleGit}/bin/git symbolic-ref --short HEAD)"
    ${roleGit}/bin/git bundle create "$tmpdir/config.bundle" HEAD "refs/heads/$branch" >/dev/null
    ${pkgs.jq}/bin/jq -c -n \
      --arg schema "airlock.config-candidate.v1" \
      --arg commit "$commit" \
      --arg branch "$branch" \
      --arg source_role "sys-config-staging" \
      '{schema: $schema, commit: $commit, branch: $branch, source_role: $source_role}' \
      > "$tmpdir/manifest.json"

    exec ${pkgs.gnutar}/bin/tar -C "$tmpdir" -cf - config.bundle manifest.json
  '';

  configCandidateExportService = pkgs.writeShellScript "airlock-config-candidate-export-qrexec" ''
    exec ${configStagingExportCandidate}/bin/airlock-config-staging-export-candidate
  '';
}
