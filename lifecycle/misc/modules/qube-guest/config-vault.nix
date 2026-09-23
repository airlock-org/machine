{
  cfg,
  pkgs,
  roleGit,
  configNix,
  configInputsPublicPath,
  configInputPathSubstitutionScript,
  initialRuntimeConfigRepoFilesScript,
  initialRuntimeConfigRepoGitAddArgs,
}:
rec {
  configVaultInit = pkgs.writeShellScriptBin "airlock-config-vault-init" ''
        set -eu

        repo="''${AIRLOCK_CONFIG_ARCHIVE_PATH:-${cfg.configArchivePath}}"
        config_inputs_root="''${AIRLOCK_CONFIG_INPUTS_PATH:-${configInputsPublicPath}}"
        tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
        trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

        ${initialRuntimeConfigRepoFilesScript}
        ${configInputPathSubstitutionScript}

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
          if ! ${roleGit}/bin/git diff --cached --quiet; then
            ${roleGit}/bin/git \
              -c user.name='Airlock Config Vault' \
              -c user.email='airlock-config-vault@localhost' \
              commit -m "$message"
          fi
        }

        ensure_archive_repo() {
          mkdir -p "$repo"
          cd "$repo"
          if [ ! -d .git ]; then
            ${roleGit}/bin/git init -b main
          fi
          mkdir -p candidates deployments
          : > candidates/.gitkeep
          : > deployments/.gitkeep
          write_archive_readme
          ${roleGit}/bin/git add README.md candidates/.gitkeep deployments/.gitkeep
        }

        set_current_link() {
          candidate_commit="$1"
          [ -d "candidates/$candidate_commit/snapshot" ] || {
            printf 'current candidate snapshot is missing: %s\n' "$candidate_commit" >&2
            exit 1
          }
          printf '%s\n' "$candidate_commit" > latest-current
          ${pkgs.coreutils}/bin/ln -sfn "candidates/$candidate_commit/snapshot" current
          ${roleGit}/bin/git add latest-current current
        }

        set_current_from_latest_deployment() {
          [ -f latest-deployment ] || return 1
          deployment_id="$(${pkgs.coreutils}/bin/head -n 1 latest-deployment)"
          [ -n "$deployment_id" ] || return 1
          [ -f "deployments/$deployment_id/deployment.json" ] || return 1
          candidate_commit="$(${pkgs.jq}/bin/jq -r '.candidate_commit // ""' "deployments/$deployment_id/deployment.json")"
          [ -n "$candidate_commit" ] && [ "$candidate_commit" != null ] || return 1
          set_current_link "$candidate_commit"
          commit_archive_if_needed "record current config from deployment $deployment_id"
          return 0
        }

        create_initial_current() {
          initial_repo="$tmpdir/initial-config"
          ${pkgs.coreutils}/bin/mkdir -p "$initial_repo"
          cd "$initial_repo"
          write_initial_runtime_config_repo_files
          substitute_config_inputs "$config_inputs_root"
          ${roleGit}/bin/git init -b main
          ${roleGit}/bin/git add ${initialRuntimeConfigRepoGitAddArgs}
          if config_inputs_ready; then
            ${configNix}/bin/airlock-config-nix \
              --extra-experimental-features nix-command \
              --extra-experimental-features flakes \
              flake lock \
              --impure \
              --offline
          else
            printf 'config vault config inputs are not present yet under %s; flake.lock deferred\n' "$config_inputs_root"
          fi
          if [ -f flake.lock ]; then
            ${roleGit}/bin/git add flake.lock
          fi
          ${roleGit}/bin/git \
            -c user.name='Airlock Initial Runtime Config' \
            -c user.email='airlock-initial-runtime-config@localhost' \
            commit -m 'init airlock config candidate'

          candidate_commit="$(${roleGit}/bin/git rev-parse HEAD)"
          archive_dir="$repo/candidates/$candidate_commit"
          snapshot_dir="$archive_dir/snapshot"
          ${pkgs.coreutils}/bin/mkdir -p "$archive_dir" "$snapshot_dir"
          ${roleGit}/bin/git bundle create "$archive_dir/config.bundle" HEAD refs/heads/main >/dev/null
          ${pkgs.jq}/bin/jq -c -n \
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
          ${roleGit}/bin/git archive --format=tar HEAD |
            ${pkgs.gnutar}/bin/tar -C "$snapshot_dir" -xf -
          ${pkgs.jq}/bin/jq -c -n \
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
          ${roleGit}/bin/git add "candidates/$candidate_commit" latest-candidate
          commit_archive_if_needed "initialize current config baseline ''${candidate_commit:0:12}"
        }

        ensure_archive_repo
        if [ -f latest-current ]; then
          current_commit="$(${pkgs.coreutils}/bin/head -n 1 latest-current)"
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
  '';

  configVaultExportCurrent = pkgs.writeShellScriptBin "airlock-config-vault-export-current" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_ARCHIVE_PATH:-${cfg.configArchivePath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    ${configVaultInit}/bin/airlock-config-vault-init >/dev/null

    cd "$repo"
    candidate_commit=""
    if [ -f latest-deployment ]; then
      deployment_id="$(${pkgs.coreutils}/bin/head -n 1 latest-deployment)"
      if [ -n "$deployment_id" ] && [ -f "deployments/$deployment_id/deployment.json" ]; then
        candidate_commit="$(${pkgs.jq}/bin/jq -r '.candidate_commit // ""' "deployments/$deployment_id/deployment.json")"
      fi
    fi
    if { [ -z "$candidate_commit" ] || [ "$candidate_commit" = null ]; } && [ -f latest-current ]; then
      candidate_commit="$(${pkgs.coreutils}/bin/head -n 1 latest-current)"
    fi
    if { [ -z "$candidate_commit" ] || [ "$candidate_commit" = null ]; } && [ -f latest-candidate ]; then
      candidate_commit="$(${pkgs.coreutils}/bin/head -n 1 latest-candidate)"
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
    ${pkgs.jq}/bin/jq -e \
      --arg commit "$candidate_commit" \
      '.schema == "airlock.config-candidate.v1" and .commit == $commit' \
      "candidates/$candidate_commit/manifest.json" >/dev/null

    ${pkgs.coreutils}/bin/cp "candidates/$candidate_commit/config.bundle" "$tmpdir/config.bundle"
    ${pkgs.coreutils}/bin/cp "candidates/$candidate_commit/manifest.json" "$tmpdir/manifest.json"
    exec ${pkgs.gnutar}/bin/tar -C "$tmpdir" -cf - config.bundle manifest.json
  '';

  configCurrentExportService = pkgs.writeShellScript "airlock-config-current-export-qrexec" ''
    exec ${configVaultExportCurrent}/bin/airlock-config-vault-export-current
  '';

  configVaultArchiveCandidate = pkgs.writeShellScriptBin "airlock-config-vault-archive-candidate" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_ARCHIVE_PATH:-${cfg.configArchivePath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    candidate_tar="$tmpdir/candidate.tar"
    ${pkgs.coreutils}/bin/cat > "$candidate_tar"
    ${pkgs.gnutar}/bin/tar -tf "$candidate_tar" |
      ${pkgs.gawk}/bin/awk '
        $0 != "config.bundle" && $0 != "validation.json" && $0 != "manifest.json" && $0 != "topology.json" && $0 != "system.json" {
          printf "unexpected candidate tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '
    ${pkgs.gnutar}/bin/tar -C "$tmpdir" -xf "$candidate_tar"

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

    manifest_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/manifest.json")"
    manifest_commit="$(${pkgs.jq}/bin/jq -r '.commit' "$tmpdir/manifest.json")"
    validation_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/validation.json")"
    validation_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/validation.json")"
    validation_commit="$(${pkgs.jq}/bin/jq -r '.commit' "$tmpdir/validation.json")"
    system_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/system.json")"
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
    ${roleGit}/bin/git init --bare -b main "$verify_repo" >/dev/null
    ${roleGit}/bin/git -C "$verify_repo" bundle verify "$tmpdir/config.bundle" > "$tmpdir/bundle-verify.log" 2>&1
    candidate_commit="$(
      ${roleGit}/bin/git -C "$verify_repo" bundle list-heads "$tmpdir/config.bundle" |
        ${pkgs.gawk}/bin/awk 'NR == 1 { print $1 }'
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
    ${roleGit}/bin/git clone "$tmpdir/config.bundle" "$snapshot_tmp" > "$tmpdir/snapshot-git-clone.log" 2>&1
    ${roleGit}/bin/git -C "$snapshot_tmp" checkout --detach "$candidate_commit" > "$tmpdir/snapshot-git-checkout.log" 2>&1
    ${pkgs.coreutils}/bin/rm -rf "$snapshot_tmp/.git"

    if [ ! -d "$repo/.git" ]; then
      ${configVaultInit}/bin/airlock-config-vault-init >/dev/null
    fi

    archive_dir="$repo/candidates/$candidate_commit"
    snapshot_dir="$archive_dir/snapshot"
    ${pkgs.coreutils}/bin/mkdir -p "$archive_dir"
    ${pkgs.coreutils}/bin/cp "$tmpdir/config.bundle" "$archive_dir/config.bundle"
    ${pkgs.coreutils}/bin/cp "$tmpdir/manifest.json" "$archive_dir/manifest.json"
    ${pkgs.coreutils}/bin/cp "$tmpdir/validation.json" "$archive_dir/validation.json"
    ${pkgs.coreutils}/bin/cp "$tmpdir/topology.json" "$archive_dir/topology.json"
    ${pkgs.coreutils}/bin/cp "$tmpdir/system.json" "$archive_dir/system.json"
    ${pkgs.coreutils}/bin/cp "$tmpdir/bundle-verify.log" "$archive_dir/bundle-verify.log"
    ${pkgs.coreutils}/bin/cp "$tmpdir/snapshot-git-clone.log" "$archive_dir/snapshot-git-clone.log"
    ${pkgs.coreutils}/bin/cp "$tmpdir/snapshot-git-checkout.log" "$archive_dir/snapshot-git-checkout.log"
    ${pkgs.coreutils}/bin/rm -rf "$snapshot_dir"
    ${pkgs.coreutils}/bin/cp -a "$snapshot_tmp" "$snapshot_dir"
    ${pkgs.jq}/bin/jq -c -n \
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
    ${roleGit}/bin/git add "candidates/$candidate_commit" latest-candidate
    archive_status=current
    if ! ${roleGit}/bin/git diff --cached --quiet; then
      ${roleGit}/bin/git \
        -c user.name='Airlock dom0 archive' \
        -c user.email='airlock-dom0@localhost' \
        commit -m "archive candidate ''${candidate_commit:0:12}" >/dev/null
      archive_status=archived
    fi

    vault_commit="$(${roleGit}/bin/git rev-parse HEAD)"
    ${pkgs.jq}/bin/jq -c -n \
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
  '';

  configVaultArchiveDeployment = pkgs.writeShellScriptBin "airlock-config-vault-archive-deployment" ''
    set -eu

    repo="''${AIRLOCK_CONFIG_ARCHIVE_PATH:-${cfg.configArchivePath}}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    deployment_tar="$tmpdir/deployment.tar"
    ${pkgs.coreutils}/bin/cat > "$deployment_tar"
    ${pkgs.gnutar}/bin/tar -tf "$deployment_tar" |
      ${pkgs.gawk}/bin/awk '
        $0 != "deployment.json" &&
        $0 != "apply.json" &&
        $0 != "archive.json" &&
        $0 != "setup.json" &&
        $0 != "topology.json" &&
        $0 != "validated-topology.json" &&
        $0 != "template-builds.json" &&
        $0 != "nix-domain-builds.json" &&
        $0 != "nix-domain-materializations.json" &&
        $0 != "unsupported-domain-backends.json" &&
        $0 != "domain-references.json" &&
        $0 != "system.json" &&
        $0 != "resolved-system.json" &&
        $0 != "system-plan.json" &&
        $0 != "system-apply.json" &&
        $0 != "system-builds.json" &&
        $0 != "systemd-actions.json" &&
        $0 != "validation.json" &&
        $0 != "manifest.json" &&
        $0 != "logs/apply.commands.log" &&
        $0 != "logs/archive.stdout.log" &&
        $0 != "logs/archive.stderr.log" &&
        $0 != "logs/setup.stdout.log" &&
        $0 != "logs/setup.stderr.log" &&
        $0 != "logs/template-builds.log" &&
        $0 != "logs/nix-domain-builds.log" &&
        $0 != "logs/system-builds.log" &&
        $0 != "logs/system-apply.log" &&
        $0 != "logs/profile-activation.log" &&
        $0 != "logs/host-system-activation.log" &&
        $0 != "logs/systemd-actions.log" &&
        $0 != "logs/clone.log" &&
        $0 != "logs/checkout.log" {
          printf "unexpected deployment tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '
    ${pkgs.gnutar}/bin/tar -C "$tmpdir" -xf "$deployment_tar"

    for required in \
      deployment.json \
      apply.json \
      archive.json \
      setup.json \
      topology.json \
      validated-topology.json \
      template-builds.json \
      nix-domain-builds.json \
      nix-domain-materializations.json \
      unsupported-domain-backends.json \
      domain-references.json \
      system.json \
      resolved-system.json \
      system-plan.json \
      system-apply.json \
      system-builds.json \
      systemd-actions.json \
      validation.json \
      manifest.json; do
      [ -f "$tmpdir/$required" ] || {
        printf 'deployment tar is missing %s\n' "$required" >&2
        exit 1
      }
    done

    deployment_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/deployment.json")"
    deployment_id="$(${pkgs.jq}/bin/jq -r '.deployment_id' "$tmpdir/deployment.json")"
    deployment_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/deployment.json")"
    candidate_commit="$(${pkgs.jq}/bin/jq -r '.candidate_commit' "$tmpdir/deployment.json")"
    apply_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/apply.json")"
    apply_commit="$(${pkgs.jq}/bin/jq -r '.candidate_commit' "$tmpdir/apply.json")"
    archive_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/archive.json")"
    archive_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/archive.json")"
    archive_commit="$(${pkgs.jq}/bin/jq -r '.candidate_commit' "$tmpdir/archive.json")"
    setup_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/setup.json")"
    topology_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/topology.json")"
    validated_topology_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/validated-topology.json")"
    template_builds_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/template-builds.json")"
    nix_domain_builds_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/nix-domain-builds.json")"
    nix_domain_materializations_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/nix-domain-materializations.json")"
    unsupported_domain_backends_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/unsupported-domain-backends.json")"
    system_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/system.json")"
    resolved_system_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/resolved-system.json")"
    system_apply_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/system-apply.json")"
    system_apply_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/system-apply.json")"
    system_builds_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/system-builds.json")"
    systemd_actions_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/systemd-actions.json")"
    validation_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/validation.json")"
    validation_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/validation.json")"
    validation_commit="$(${pkgs.jq}/bin/jq -r '.commit' "$tmpdir/validation.json")"

    domain_references_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/domain-references.json")"
    domain_references_status="$(${pkgs.jq}/bin/jq -r '.status' "$tmpdir/domain-references.json")"

    [ "$deployment_schema" = "airlock.config-deployment.v1" ] || {
      printf 'unsupported deployment schema: %s\n' "$deployment_schema" >&2
      exit 1
    }
    case "$deployment_id" in
      ""|*[!A-Za-z0-9_.:-]*)
        printf 'invalid deployment id: %s\n' "$deployment_id" >&2
        exit 1
        ;;
    esac
    case "$deployment_status" in
      passed|failed) ;;
      *)
        printf 'invalid deployment status: %s\n' "$deployment_status" >&2
        exit 1
        ;;
    esac
    [ -n "$candidate_commit" ] && [ "$candidate_commit" != null ] || {
      printf 'deployment record is missing candidate_commit\n' >&2
      exit 1
    }
    [ "$apply_schema" = "airlock.config-apply.v1" ] || {
      printf 'unsupported apply schema: %s\n' "$apply_schema" >&2
      exit 1
    }
    [ "$archive_schema" = "airlock.config-archive.v1" ] || {
      printf 'unsupported archive schema: %s\n' "$archive_schema" >&2
      exit 1
    }
    [ "$archive_status" = passed ] || {
      printf 'candidate archive did not pass: %s\n' "$archive_status" >&2
      exit 1
    }
    [ "$setup_schema" = "airlock.setup-config.v1" ] || {
      printf 'unsupported setup schema: %s\n' "$setup_schema" >&2
      exit 1
    }
    [ "$topology_schema" = "airlock.qubes-topology.v1" ] || {
      printf 'unsupported topology schema: %s\n' "$topology_schema" >&2
      exit 1
    }
    [ "$validated_topology_schema" = "airlock.qubes-topology.v1" ] || {
      printf 'unsupported validated topology schema: %s\n' "$validated_topology_schema" >&2
      exit 1
    }
    [ "$template_builds_schema" = "airlock.template-builds.v1" ] || {
      printf 'unsupported template builds schema: %s\n' "$template_builds_schema" >&2
      exit 1
    }
    [ "$nix_domain_builds_schema" = "airlock.nix-domain-builds.v1" ] || {
      printf 'unsupported Nix-native domain builds schema: %s\n' "$nix_domain_builds_schema" >&2
      exit 1
    }
    [ "$nix_domain_materializations_schema" = "airlock.nix-domain-materializations.v1" ] || {
      printf 'unsupported Nix-native domain materializations schema: %s\n' "$nix_domain_materializations_schema" >&2
      exit 1
    }
    [ "$unsupported_domain_backends_schema" = "airlock.unsupported-domain-backends.v1" ] || {
      printf 'unsupported domain backend report schema: %s\n' "$unsupported_domain_backends_schema" >&2
      exit 1
    }
    [ "$domain_references_schema" = "airlock.domain-references.v1" ] || {
      printf 'unsupported domain references schema: %s\n' "$domain_references_schema" >&2
      exit 1
    }
    [ "$domain_references_status" = passed ] || {
      printf 'domain references did not pass: %s\n' "$domain_references_status" >&2
      exit 1
    }
    [ "$system_schema" = "airlock.system.v1" ] || {
      printf 'unsupported system schema: %s\n' "$system_schema" >&2
      exit 1
    }
    [ "$resolved_system_schema" = "airlock.system.v1" ] || {
      printf 'unsupported resolved system schema: %s\n' "$resolved_system_schema" >&2
      exit 1
    }
    [ "$system_apply_schema" = "airlock.system-apply.v1" ] || {
      printf 'unsupported system apply schema: %s\n' "$system_apply_schema" >&2
      exit 1
    }
    [ "$system_apply_status" = passed ] || {
      printf 'system apply did not pass: %s\n' "$system_apply_status" >&2
      exit 1
    }
    [ "$system_builds_schema" = "airlock.system-builds.v1" ] || {
      printf 'unsupported system builds schema: %s\n' "$system_builds_schema" >&2
      exit 1
    }
    [ "$systemd_actions_schema" = "airlock.systemd-actions.v1" ] || {
      printf 'unsupported systemd actions schema: %s\n' "$systemd_actions_schema" >&2
      exit 1
    }
    [ "$validation_schema" = "airlock.config-validation.v1" ] || {
      printf 'unsupported validation schema: %s\n' "$validation_schema" >&2
      exit 1
    }
    [ "$validation_status" = passed ] || {
      printf 'candidate validation did not pass: %s\n' "$validation_status" >&2
      exit 1
    }
    [ "$apply_commit" = "$candidate_commit" ] || {
      printf 'apply commit %s does not match deployment commit %s\n' \
        "$apply_commit" \
        "$candidate_commit" >&2
      exit 1
    }
    [ "$archive_commit" = "$candidate_commit" ] || {
      printf 'archive commit %s does not match deployment commit %s\n' \
        "$archive_commit" \
        "$candidate_commit" >&2
      exit 1
    }
    [ "$validation_commit" = "$candidate_commit" ] || {
      printf 'validation commit %s does not match deployment commit %s\n' \
        "$validation_commit" \
        "$candidate_commit" >&2
      exit 1
    }

    if [ ! -d "$repo/.git" ]; then
      ${configVaultInit}/bin/airlock-config-vault-init >/dev/null
    fi

    [ -d "$repo/candidates/$candidate_commit" ] || {
      printf 'deployment references missing candidate archive: %s\n' "$candidate_commit" >&2
      exit 1
    }

    deployment_dir="$repo/deployments/$deployment_id"
    [ ! -e "$deployment_dir" ] || {
      printf 'deployment already exists: %s\n' "$deployment_id" >&2
      exit 1
    }

    ${pkgs.coreutils}/bin/mkdir -p "$deployment_dir/logs"
    for file in \
      deployment.json \
      apply.json \
      archive.json \
      setup.json \
      topology.json \
      validated-topology.json \
      template-builds.json \
      nix-domain-builds.json \
      nix-domain-materializations.json \
      unsupported-domain-backends.json \
      domain-references.json \
      system.json \
      resolved-system.json \
      system-plan.json \
      system-apply.json \
      system-builds.json \
      systemd-actions.json \
      validation.json \
      manifest.json; do
      ${pkgs.coreutils}/bin/cp "$tmpdir/$file" "$deployment_dir/$file"
    done
    for log in \
      apply.commands.log \
      archive.stdout.log \
      archive.stderr.log \
      setup.stdout.log \
      setup.stderr.log \
      template-builds.log \
      nix-domain-builds.log \
      system-builds.log \
      system-apply.log \
      profile-activation.log \
      host-system-activation.log \
      systemd-actions.log \
      clone.log \
      checkout.log; do
      ${pkgs.coreutils}/bin/cp "$tmpdir/logs/$log" "$deployment_dir/logs/$log"
    done

    printf '%s\n' "$deployment_id" > "$repo/latest-deployment-attempt"
    if [ "$deployment_status" = passed ]; then
      printf '%s\n' "$deployment_id" > "$repo/latest-deployment"
      printf '%s\n' "$candidate_commit" > "$repo/latest-current"
      ${pkgs.coreutils}/bin/ln -sfn "candidates/$candidate_commit/snapshot" "$repo/current"
    fi

    cd "$repo"
    ${roleGit}/bin/git add "deployments/$deployment_id" latest-deployment-attempt
    if [ "$deployment_status" = passed ]; then
      ${roleGit}/bin/git add latest-deployment latest-current current
    fi
    ${roleGit}/bin/git \
      -c user.name='Airlock dom0 archive' \
      -c user.email='airlock-dom0@localhost' \
      commit -m "record deployment $deployment_id" >/dev/null

    vault_commit="$(${roleGit}/bin/git rev-parse HEAD)"
    ${pkgs.jq}/bin/jq -c -n \
      --arg schema "airlock.config-vault-deployment.v1" \
      --arg status "passed" \
      --arg deployment_status "$deployment_status" \
      --arg deployment_id "$deployment_id" \
      --arg candidate_commit "$candidate_commit" \
      --arg vault_commit "$vault_commit" \
      --arg archive_repo "$repo" \
      '{
        schema: $schema,
        status: $status,
        deployment_status: $deployment_status,
        deployment_id: $deployment_id,
        candidate_commit: $candidate_commit,
        vault_commit: $vault_commit,
        archive_repo: $archive_repo
      }'
  '';

  configArchiveService = pkgs.writeShellScript "airlock-config-archive-candidate-qrexec" ''
    exec ${configVaultArchiveCandidate}/bin/airlock-config-vault-archive-candidate
  '';

  configDeploymentArchiveService = pkgs.writeShellScript "airlock-config-archive-deployment-qrexec" ''
    exec ${configVaultArchiveDeployment}/bin/airlock-config-vault-archive-deployment
  '';
}
