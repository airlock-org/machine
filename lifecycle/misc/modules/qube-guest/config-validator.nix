{
  cfg,
  pkgs,
  roleGit,
  configNix,
}:
rec {
  configValidatorInit = pkgs.writeShellScriptBin "airlock-config-validator-init" ''
    set -eu

    validation_dir="''${AIRLOCK_CONFIG_VALIDATION_DIR:-${cfg.configValidationPath}}"
    mkdir -p "$validation_dir/logs"
    printf 'config validator workspace ready: %s\n' "$validation_dir"
  '';

  configValidatorValidateCandidate = pkgs.writeShellScriptBin "airlock-config-validator-validate-candidate" ''
    set -eu

    validation_dir="''${AIRLOCK_CONFIG_VALIDATION_DIR:-${cfg.configValidationPath}}"
    run_full_flake_check="''${AIRLOCK_CONFIG_VALIDATION_BUILD:-0}"
    tmpdir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -rf "$tmpdir"' EXIT

    candidate_tar="$tmpdir/candidate.tar"
    ${pkgs.coreutils}/bin/cat > "$candidate_tar"
    case "$run_full_flake_check" in
      0|1)
        ;;
      *)
        printf 'AIRLOCK_CONFIG_VALIDATION_BUILD must be 0 or 1\n' >&2
        exit 2
        ;;
    esac
    ${pkgs.gnutar}/bin/tar -tf "$candidate_tar" |
      ${pkgs.gawk}/bin/awk '
        $0 != "config.bundle" && $0 != "manifest.json" {
          printf "unexpected staged candidate tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '
    ${pkgs.gnutar}/bin/tar -C "$tmpdir" -xf "$candidate_tar"

    [ -f "$tmpdir/config.bundle" ] || {
      printf 'staged candidate tar is missing config.bundle\n' >&2
      exit 1
    }
    [ -f "$tmpdir/manifest.json" ] || {
      printf 'staged candidate tar is missing manifest.json\n' >&2
      exit 1
    }

    manifest_schema="$(${pkgs.jq}/bin/jq -r '.schema' "$tmpdir/manifest.json")"
    manifest_commit="$(${pkgs.jq}/bin/jq -r '.commit' "$tmpdir/manifest.json")"
    [ "$manifest_schema" = "airlock.config-candidate.v1" ] || {
      printf 'unsupported candidate manifest schema: %s\n' "$manifest_schema" >&2
      exit 1
    }
    [ -n "$manifest_commit" ] && [ "$manifest_commit" != null ] || {
      printf 'candidate manifest is missing commit\n' >&2
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
    [ "$candidate_commit" = "$manifest_commit" ] || {
      printf 'candidate commit %s does not match manifest commit %s\n' "$candidate_commit" "$manifest_commit" >&2
      exit 1
    }

    worktree="$tmpdir/worktree"
    ${roleGit}/bin/git clone "$tmpdir/config.bundle" "$worktree" > "$tmpdir/git-clone.log" 2>&1
    cd "$worktree"
    checked_commit="$(${roleGit}/bin/git rev-parse HEAD)"
    dirty="$(${roleGit}/bin/git status --porcelain)"
    [ "$checked_commit" = "$candidate_commit" ] || {
      printf 'cloned commit %s does not match candidate commit %s\n' "$checked_commit" "$candidate_commit" >&2
      exit 1
    }

    mkdir -p "$validation_dir/logs"
    result_file="$validation_dir/$candidate_commit.json"
    result_tmp="$result_file.tmp"
    flake_log="$validation_dir/logs/flake-check-$candidate_commit.log"
    flake_tests_log="$validation_dir/logs/flake-check-build-$candidate_commit.log"
    topology_eval_log="$validation_dir/logs/nix-eval-topology-$candidate_commit.log"
    topology_log="$validation_dir/logs/topology-json-$candidate_commit.log"
    template_refs_log="$validation_dir/logs/template-source-refs-$candidate_commit.log"
    system_eval_log="$validation_dir/logs/nix-eval-system-$candidate_commit.log"
    system_log="$validation_dir/logs/system-json-$candidate_commit.log"
    system_refs_log="$validation_dir/logs/system-source-refs-$candidate_commit.log"
    local_flake_log="$tmpdir/flake-check.log"
    local_flake_tests_log="$tmpdir/flake-check-build.log"
    local_topology_eval_log="$tmpdir/nix-eval-topology.log"
    local_topology_log="$tmpdir/topology-json.log"
    local_template_refs_log="$tmpdir/template-source-refs.log"
    local_system_eval_log="$tmpdir/nix-eval-system.log"
    local_system_log="$tmpdir/system-json.log"
    local_system_refs_log="$tmpdir/system-source-refs.log"
    generated_topology="$tmpdir/generated-topology.json"
    generated_system="$tmpdir/generated-system.json"

    write_validation() {
      status="$1"
      reason="$2"
      topology_eval_status="$3"
      topology_status="$4"
      topology_path="$5"
      topology_eval_log_path="$6"
      topology_log_path="$7"
      template_refs_status="$8"
      template_refs_log_path="$9"
      system_eval_status="''${10}"
      system_status="''${11}"
      system_path="''${12}"
      system_eval_log_path="''${13}"
      system_log_path="''${14}"
      system_refs_status="''${15}"
      system_refs_log_path="''${16}"
      flake_status="''${17}"
      flake_tests_status="''${18}"

      ${pkgs.jq}/bin/jq -c -n \
        --arg schema "airlock.config-validation.v1" \
        --arg status "$status" \
        --arg reason "$reason" \
        --arg validator_role "sys-config-validator" \
        --arg commit "$candidate_commit" \
        --arg dirty "$dirty" \
        --arg manifest "$tmpdir/manifest.json" \
        --arg bundle_verify_log "$tmpdir/bundle-verify.log" \
        --arg git_clone_log "$tmpdir/git-clone.log" \
        --arg topology_eval_status "$topology_eval_status" \
        --arg topology_status "$topology_status" \
        --arg topology_path "$topology_path" \
        --arg topology_eval_log "$topology_eval_log_path" \
        --arg topology_log "$topology_log_path" \
        --arg template_refs_status "$template_refs_status" \
        --arg template_refs_log "$template_refs_log_path" \
        --arg system_eval_status "$system_eval_status" \
        --arg system_status "$system_status" \
        --arg system_path "$system_path" \
        --arg system_eval_log "$system_eval_log_path" \
        --arg system_log "$system_log_path" \
        --arg system_refs_status "$system_refs_status" \
        --arg system_refs_log "$system_refs_log_path" \
        --arg flake_status "$flake_status" \
        --arg flake_log "$flake_log" \
        --arg flake_tests_status "$flake_tests_status" \
        --arg flake_tests_log "$flake_tests_log" \
        '{
          schema: $schema,
          status: $status,
          reason: $reason,
          validator_role: $validator_role,
          commit: $commit,
          checks: [
            {
              name: "candidate-manifest",
              status: "passed",
              manifest: $manifest
            },
            {
              name: "git-bundle-verify",
              status: "passed",
              log: $bundle_verify_log
            },
            {
              name: "git-clone",
              status: "passed",
              log: $git_clone_log
            },
            {
              name: "git-clean",
              status: (if $dirty == "" then "passed" else "failed" end),
              details: $dirty
            },
            {
              name: "nix-eval-airlock-topology",
              status: $topology_eval_status,
              attr: "airlock.topology",
              output: $topology_path,
              log: $topology_eval_log
            },
            {
              name: "airlock-topology-json",
              status: $topology_status,
              path: $topology_path,
              log: $topology_log
            },
            {
              name: "nix-template-source-refs-dry-run",
              status: $template_refs_status,
              log: $template_refs_log
            },
            {
              name: "nix-eval-airlock-system",
              status: $system_eval_status,
              attr: "airlock.system",
              output: $system_path,
              log: $system_eval_log
            },
            {
              name: "airlock-system-json",
              status: $system_status,
              path: $system_path,
              log: $system_log
            },
            {
              name: "nix-system-source-refs-dry-run",
              status: $system_refs_status,
              log: $system_refs_log
            },
            {
              name: "nix-flake-check-no-build",
              status: $flake_status,
              log: $flake_log
            },
            {
              name: "nix-flake-check",
              status: $flake_tests_status,
              log: $flake_tests_log
            }
          ]
        }' > "$result_tmp"
      mv "$result_tmp" "$result_file"
      ${pkgs.coreutils}/bin/cp "$result_file" "$tmpdir/validation.json"
    }

    if [ -n "$dirty" ]; then
      write_validation failed "validated candidate worktree is dirty" skipped skipped "topology.json" "$topology_eval_log" "$topology_log" skipped "$template_refs_log" skipped skipped "system.json" "$system_eval_log" "$system_log" skipped "$system_refs_log" skipped skipped
      cat "$tmpdir/validation.json" >&2
      exit 1
    fi

    mark_failed() {
      if [ "$status" = passed ]; then
        reason="$1"
      fi
      status=failed
    }

    topology_eval_status=skipped
    topology_status=skipped
    template_refs_status=skipped
    system_eval_status=skipped
    system_status=skipped
    system_refs_status=skipped
    flake_status=skipped
    flake_tests_status=skipped
    reason="validation passed"
    status=passed

    check_template_source_refs() {
      topology_file="$1"
      log="$2"

      : > "$log"
      refs="$(
        ${pkgs.jq}/bin/jq -r '
          [
            (
              (.templates // {})
              | to_entries[]
              | select(.value.source_ref != null)
              | {
                  kind: "template",
                  name: .key,
                  ref: .value.source_ref
                }
            ),
            (
              (.domains // {})
              | to_entries[]
              | . as $domain
              | ($domain.value.backend // {}) as $backend
              | select($backend.type == "nix-closure")
              | [
                  { component: "system", ref: ($backend.system.source_ref // null) },
                  { component: "kernel", ref: ($backend.kernel.source_ref // null) },
                  { component: "initrd", ref: ($backend.initrd.source_ref // null) },
                  { component: "modules", ref: ($backend.modules.source_ref // null) },
                  { component: "root", ref: ($backend.root.source_ref // null) },
                  { component: "store", ref: ($backend.store.source_ref // null) }
                ]
              | .[]
              | select(.ref != null)
              | {
                  kind: "nix-domain",
                  name: ($domain.key + "." + .component),
                  ref: .ref
                }
            )
          ]
          | .[]
          | @base64
        ' "$topology_file"
      )"

      if [ -z "$refs" ]; then
        printf 'no topology source refs declared\n' >> "$log"
        return 0
      fi

      printf '%s\n' "$refs" |
        while IFS= read -r encoded; do
          [ -n "$encoded" ] || continue
          ref_json="$(printf '%s' "$encoded" | ${pkgs.coreutils}/bin/base64 -d)"
          kind="$(printf '%s' "$ref_json" | ${pkgs.jq}/bin/jq -r '.kind')"
          name="$(printf '%s' "$ref_json" | ${pkgs.jq}/bin/jq -r '.name')"
          attr="$(printf '%s' "$ref_json" | ${pkgs.jq}/bin/jq -r '.ref.attr')"
          printf 'checking %s=%s attr=%s\n' "$kind" "$name" "$attr" >> "$log"
          ${configNix}/bin/airlock-config-nix \
            --extra-experimental-features nix-command \
            --extra-experimental-features flakes \
            build \
            --impure \
            --offline \
            --no-write-lock-file \
            --dry-run \
            --no-link \
            --show-trace \
            ".#$attr" >> "$log" 2>&1
        done
    }

    check_system_source_refs() {
      system_file="$1"
      log="$2"

      : > "$log"
      refs="$(
        ${pkgs.jq}/bin/jq -r '
          [
            { name: "dom0.profile", ref: .dom0.profile.source_ref },
            { name: "dom0.nixos.system", ref: .dom0.nixos.system.source_ref },
            { name: "dom0.nixos.kernel", ref: .dom0.nixos.kernel.source_ref },
            { name: "dom0.nixos.xen", ref: .dom0.nixos.xen.source_ref },
            { name: "dom0.nixos.bootloader", ref: .dom0.nixos.bootloader.source_ref },
            { name: "dom0.nixos.qubes", ref: .dom0.nixos.qubes.source_ref }
          ]
          | .[]
          | select(.ref != null)
          | @base64
        ' "$system_file"
      )"
      direct_sources="$(
        ${pkgs.jq}/bin/jq -r '
          [
            {
              component: "dom0.profile",
              source: .dom0.profile.source,
              activation_command: .dom0.profile.activation_command
            },
            {
              component: "dom0.nixos.system",
              source: .dom0.nixos.system.source,
              activation_command: null
            },
            {
              component: "dom0.nixos.kernel",
              source: .dom0.nixos.kernel.source,
              activation_command: null
            },
            {
              component: "dom0.nixos.xen",
              source: .dom0.nixos.xen.source,
              activation_command: null
            },
            {
              component: "dom0.nixos.bootloader",
              source: .dom0.nixos.bootloader.source,
              activation_command: null
            },
            {
              component: "dom0.nixos.qubes",
              source: .dom0.nixos.qubes.source,
              activation_command: null
            }
          ]
          | .[]
          | select(.source != null)
          | @base64
        ' "$system_file"
      )"

      if [ -z "$refs" ] && [ -z "$direct_sources" ]; then
        printf 'no system sources declared\n' >> "$log"
        return 0
      fi

      if [ -n "$refs" ]; then
        printf '%s\n' "$refs" |
          while IFS= read -r encoded; do
            [ -n "$encoded" ] || continue
            ref_json="$(printf '%s' "$encoded" | ${pkgs.coreutils}/bin/base64 -d)"
            name="$(printf '%s' "$ref_json" | ${pkgs.jq}/bin/jq -r '.name')"
            attr="$(printf '%s' "$ref_json" | ${pkgs.jq}/bin/jq -r '.ref.attr')"
            printf 'checking system=%s attr=%s\n' "$name" "$attr" >> "$log"
            ${configNix}/bin/airlock-config-nix \
              --extra-experimental-features nix-command \
              --extra-experimental-features flakes \
              build \
              --impure \
              --offline \
              --no-write-lock-file \
              --dry-run \
              --no-link \
              --show-trace \
              ".#$attr" >> "$log" 2>&1
          done
      fi

      if [ -n "$direct_sources" ]; then
        printf '%s\n' "$direct_sources" |
          while IFS= read -r encoded; do
          [ -n "$encoded" ] || continue
          source_json="$(printf '%s' "$encoded" | ${pkgs.coreutils}/bin/base64 -d)"
          component="$(printf '%s' "$source_json" | ${pkgs.jq}/bin/jq -r '.component')"
          source="$(printf '%s' "$source_json" | ${pkgs.jq}/bin/jq -r '.source')"
          activation_command="$(printf '%s' "$source_json" | ${pkgs.jq}/bin/jq -r '.activation_command // ""')"
          case "$source" in
            "")
              printf 'empty direct system source for %s\n' "$component" >> "$log"
              exit 1
              ;;
            /*)
              resolved="$source"
              ;;
            .|./|..|../*|*/..|*/../*|*//*)
              printf 'unsafe relative system source for %s: %s\n' "$component" "$source" >> "$log"
              exit 1
              ;;
            ./*)
              stripped="$(printf '%s' "$source" | ${pkgs.gnused}/bin/sed 's|^\./||')"
              resolved="$PWD/$stripped"
              ;;
            *)
              resolved="$PWD/$source"
              ;;
          esac
          printf 'checking system=%s source=%s resolved=%s\n' "$component" "$source" "$resolved" >> "$log"
          [ -e "$resolved" ] || {
            printf 'direct system source is missing for %s: %s\n' "$component" "$source" >> "$log"
            exit 1
          }
          if [ "$component" = "dom0.profile" ]; then
            [ -d "$resolved" ] || {
              printf 'direct dom0 profile source is not a directory: %s\n' "$source" >> "$log"
              exit 1
            }
            [ -n "$activation_command" ] || activation_command="bin/airlock-dom0-profile-activate"
            [ -x "$resolved/$activation_command" ] || {
              printf 'direct dom0 profile activation command is missing or not executable: %s/%s\n' "$source" "$activation_command" >> "$log"
              exit 1
            }
          fi
          if [ "$component" = "dom0.nixos.system" ]; then
            [ -d "$resolved" ] || {
              printf 'dom0 NixOS system source is not a directory: %s\n' "$source" >> "$log"
              exit 1
            }
            [ -x "$resolved/bin/switch-to-configuration" ] || {
              printf 'dom0 NixOS system source is missing bin/switch-to-configuration: %s\n' "$source" >> "$log"
              exit 1
            }
          fi
          done
      fi
    }

    if [ -f flake.nix ]; then
      if ${configNix}/bin/airlock-config-nix \
        --extra-experimental-features nix-command \
        --extra-experimental-features flakes \
        flake check \
        --impure \
        --offline \
        --no-write-lock-file \
        --no-build . > "$local_flake_log" 2>&1; then
        flake_status=passed
      else
        flake_status=failed
        mark_failed "nix flake check --no-build failed"
      fi
      ${pkgs.coreutils}/bin/cp "$local_flake_log" "$flake_log"
      if [ "$flake_status" = passed ] && [ "$run_full_flake_check" = 1 ]; then
        if ${configNix}/bin/airlock-config-nix \
          --extra-experimental-features nix-command \
          --extra-experimental-features flakes \
          flake check \
          --impure \
          --offline \
          --no-write-lock-file \
          . > "$local_flake_tests_log" 2>&1; then
          flake_tests_status=passed
        else
          flake_tests_status=failed
          mark_failed "nix flake check failed"
        fi
        ${pkgs.coreutils}/bin/cp "$local_flake_tests_log" "$flake_tests_log"
      elif [ "$flake_status" = passed ]; then
        printf 'skipped offline full flake build; set AIRLOCK_CONFIG_VALIDATION_BUILD=1 when all build inputs are preloaded\n' > "$flake_tests_log"
      else
        printf 'skipped because nix flake check --no-build did not pass\n' > "$flake_tests_log"
      fi
    else
      printf 'no flake.nix present; cannot evaluate airlock.topology\n' > "$flake_log"
      printf 'no flake.nix present; cannot run nix flake check\n' > "$flake_tests_log"
      flake_status=failed
      flake_tests_status=failed
      mark_failed "flake.nix is missing"
    fi

    if [ -f flake.nix ]; then
      if ${configNix}/bin/airlock-config-nix \
        --extra-experimental-features nix-command \
        --extra-experimental-features flakes \
        eval \
        --impure \
        --offline \
        --no-write-lock-file \
        .#airlock.topology --json > "$generated_topology" 2> "$local_topology_eval_log"; then
        topology_eval_status=passed
      else
        topology_eval_status=failed
        : > "$generated_topology"
        mark_failed "nix eval .#airlock.topology failed"
      fi
      ${pkgs.coreutils}/bin/cp "$local_topology_eval_log" "$topology_eval_log"
    else
      printf 'no flake.nix present; cannot evaluate airlock.topology\n' > "$topology_eval_log"
    fi

    if [ "$topology_eval_status" = passed ]; then
      if ${pkgs.jq}/bin/jq -e '
        .schema == "airlock.qubes-topology.v1" and
        (.templates | type == "object") and
        (.kernels | type == "object") and
        (.prefs | type == "object") and
        (.domains | type == "object") and
        (.qrexec_policy | type == "object") and
        (.qrexec_policy.rules | type == "array")
      ' "$generated_topology" > "$local_topology_log" 2>&1; then
        topology_status=passed
      else
          topology_status=failed
          mark_failed "evaluated airlock.topology JSON validation failed"
        fi
        ${pkgs.coreutils}/bin/cp "$local_topology_log" "$topology_log"

        if check_template_source_refs "$generated_topology" "$local_template_refs_log"; then
          template_refs_status=passed
        else
          template_refs_status=failed
          mark_failed "template source reference dry-run failed"
        fi
        ${pkgs.coreutils}/bin/cp "$local_template_refs_log" "$template_refs_log"
      else
        printf 'skipped because nix eval .#airlock.topology did not pass\n' > "$topology_log"
        printf 'skipped because nix eval .#airlock.topology did not pass\n' > "$template_refs_log"
      fi

      if ${configNix}/bin/airlock-config-nix \
        --extra-experimental-features nix-command \
        --extra-experimental-features flakes \
        eval \
        --impure \
        --offline \
        --no-write-lock-file \
        .#airlock.system --json > "$generated_system" 2> "$local_system_eval_log"; then
        system_eval_status=passed
      else
        system_eval_status=failed
        : > "$generated_system"
        mark_failed "nix eval .#airlock.system failed"
      fi
      ${pkgs.coreutils}/bin/cp "$local_system_eval_log" "$system_eval_log"

      if [ "$system_eval_status" = passed ]; then
        if ${pkgs.jq}/bin/jq -e '
          .schema == "airlock.system.v1" and
          (.dom0 | type == "object") and
          (.plan | type == "object") and
          (.plan.live_actions | type == "array") and
          (.plan.reboot_actions | type == "array") and
          (.plan.reboot_required | type == "boolean") and
          (.plan.reboot_reasons | type == "array")
        ' "$generated_system" > "$local_system_log" 2>&1; then
          system_status=passed
        else
          system_status=failed
          mark_failed "evaluated airlock.system JSON validation failed"
        fi
        ${pkgs.coreutils}/bin/cp "$local_system_log" "$system_log"

        if check_system_source_refs "$generated_system" "$local_system_refs_log"; then
          system_refs_status=passed
        else
          system_refs_status=failed
          mark_failed "system source reference dry-run failed"
        fi
        ${pkgs.coreutils}/bin/cp "$local_system_refs_log" "$system_refs_log"
      else
        printf 'skipped because nix eval .#airlock.system did not pass\n' > "$system_log"
        printf 'skipped because nix eval .#airlock.system did not pass\n' > "$system_refs_log"
      fi

    write_validation "$status" "$reason" "$topology_eval_status" "$topology_status" "topology.json" "$topology_eval_log" "$topology_log" "$template_refs_status" "$template_refs_log" "$system_eval_status" "$system_status" "system.json" "$system_eval_log" "$system_log" "$system_refs_status" "$system_refs_log" "$flake_status" "$flake_tests_status"
    if [ "$status" != passed ]; then
      cat "$tmpdir/validation.json" >&2
      exit 1
    fi

    ${pkgs.coreutils}/bin/cp "$generated_topology" "$tmpdir/topology.json"
    ${pkgs.coreutils}/bin/cp "$generated_system" "$tmpdir/system.json"
    exec ${pkgs.gnutar}/bin/tar -C "$tmpdir" -cf - config.bundle validation.json manifest.json topology.json system.json
  '';

  configCandidateValidateService = pkgs.writeShellScript "airlock-config-candidate-validate-qrexec" ''
    exec ${configValidatorValidateCandidate}/bin/airlock-config-validator-validate-candidate
  '';
}
