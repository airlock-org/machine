{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.qubeGuest;
  role = cfg.role or "usr-assistant";
  repo = cfg.configRepoPath;
  state = "/home/${cfg.user}/.local/share/airlock/machine-build";
  cache = "/home/${cfg.user}/.cache/airlock-machine-source";
  sourceTool = pkgs.writeShellScriptBin "airlock-usr-assistant-machine-source" ''
    set -eu

    usage() {
      printf '%s\n' \
        'usage: airlock-usr-assistant-machine-source import --revision REV --lock-hash HASH < source.tar.gz' \
        '       airlock-usr-assistant-machine-source status'
    }

    fail() {
      printf 'airlock-usr-assistant-machine-source: %s\n' "$*" >&2
      exit 1
    }

    command="''${1:-}"
    [ -n "$command" ] || { usage >&2; exit 2; }
    shift

    case "$command" in
      status)
        [ "$#" -eq 0 ] || { usage >&2; exit 2; }
        if [ -d ${lib.escapeShellArg "${repo}/.git"} ]; then
          revision="$(${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} rev-parse HEAD)"
          lock_hash="$(${pkgs.coreutils}/bin/sha256sum ${lib.escapeShellArg "${repo}/flake.lock"} | ${pkgs.gawk}/bin/awk '{print $1}')"
          dirty="$(${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} status --porcelain | ${pkgs.coreutils}/bin/wc -l)"
          ${pkgs.jq}/bin/jq -n \
            --arg repo ${lib.escapeShellArg repo} \
            --arg revision "$revision" \
            --arg lockHash "$lock_hash" \
            --arg seedRevision "$(${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} config --get airlock.seedRevision || true)" \
            --arg seedLockHash "$(${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} config --get airlock.seedLockHash || true)" \
            --argjson dirty "$dirty" \
            '{schema: "airlock.sys-agent-machine-source-status.v1", present: true, repo: $repo, revision: $revision, lock_hash: $lockHash, seed_revision: $seedRevision, seed_lock_hash: $seedLockHash, dirty_paths: $dirty}'
        else
          ${pkgs.jq}/bin/jq -n --arg repo ${lib.escapeShellArg repo} \
            '{schema: "airlock.sys-agent-machine-source-status.v1", present: false, repo: $repo}'
        fi
        ;;
      import)
        expected_revision=""
        expected_lock_hash=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --revision)
              expected_revision="''${2:-}"
              shift 2
              ;;
            --lock-hash)
              expected_lock_hash="''${2:-}"
              shift 2
              ;;
            *)
              usage >&2
              exit 2
              ;;
          esac
        done
        printf '%s' "$expected_revision" | ${pkgs.gnugrep}/bin/grep -Eq '^[0-9a-f]{40,64}$' || fail 'invalid revision'
        printf '%s' "$expected_lock_hash" | ${pkgs.gnugrep}/bin/grep -Eq '^[0-9a-f]{64}$' || fail 'invalid lock hash'
        ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg cache} ${lib.escapeShellArg state} ${lib.escapeShellArg (builtins.dirOf repo)}
        temp="$(${pkgs.coreutils}/bin/mktemp -d ${lib.escapeShellArg "${cache}/import.XXXXXX"})"
        trap '${pkgs.coreutils}/bin/rm -rf "$temp"' EXIT
        archive="$temp/source.tar.gz"
        ${pkgs.coreutils}/bin/cat > "$archive"
        [ -s "$archive" ] || fail 'source archive is empty'
        ${pkgs.gnutar}/bin/tar -tzf "$archive" | while IFS= read -r entry; do
          case "$entry" in
            manifest.json|repo.bundle|submodules/*.bundle) ;;
            *) fail "unexpected archive member: $entry" ;;
          esac
        done
        ${pkgs.gnutar}/bin/tar -xzf "$archive" -C "$temp" --no-same-owner --no-same-permissions
        [ -f "$temp/manifest.json" ] && [ -f "$temp/repo.bundle" ] || fail 'source archive is incomplete'
        [ "$(${pkgs.jq}/bin/jq -r '.schema // empty' "$temp/manifest.json")" = 'airlock.sys-agent-machine-source.v1' ] || fail 'unsupported source archive schema'
        [ "$(${pkgs.jq}/bin/jq -r '.revision // empty' "$temp/manifest.json")" = "$expected_revision" ] || fail 'archive revision differs from requested revision'
        [ "$(${pkgs.jq}/bin/jq -r '.lock_hash // empty' "$temp/manifest.json")" = "$expected_lock_hash" ] || fail 'archive lock hash differs from requested lock hash'
        verify_repo="$temp/verify"
        ${pkgs.gitMinimal}/bin/git init -q "$verify_repo"
        ${pkgs.gitMinimal}/bin/git -C "$verify_repo" bundle verify "$temp/repo.bundle" >/dev/null
        staged="$temp/repo"
        ${pkgs.gitMinimal}/bin/git clone --no-checkout "$temp/repo.bundle" "$staged" >/dev/null
        ${pkgs.gitMinimal}/bin/git -C "$staged" checkout --detach "$expected_revision" >/dev/null
        [ "$(${pkgs.coreutils}/bin/sha256sum "$staged/flake.lock" | ${pkgs.gawk}/bin/awk '{print $1}')" = "$expected_lock_hash" ] || fail 'checked out flake.lock differs from requested lock hash'
        submodule_count="$(${pkgs.jq}/bin/jq -r '(.submodules // []) | length' "$temp/manifest.json")"
        [ "$submodule_count" = 0 ] || fail 'source archive submodules are not yet supported by this image'
        if [ -e ${lib.escapeShellArg repo} ]; then
          [ -d ${lib.escapeShellArg "${repo}/.git"} ] || fail 'checkout path exists but is not a Git repository'
          [ -z "$(${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} status --porcelain)" ] || fail 'checkout has local edits; refusing to overwrite it'
          ${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} fetch "$temp/repo.bundle" "$expected_revision" >/dev/null
          ${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} checkout --detach "$expected_revision" >/dev/null
        else
          ${pkgs.coreutils}/bin/mv "$staged" ${lib.escapeShellArg repo}
        fi
        ${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} config airlock.seedRevision "$expected_revision"
        ${pkgs.gitMinimal}/bin/git -C ${lib.escapeShellArg repo} config airlock.seedLockHash "$expected_lock_hash"
        exec "$0" status
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
  '';
  buildTool = pkgs.writeShellScriptBin "airlock-usr-assistant-machine-build" ''
    set -euo pipefail
    # build_installable runs inside command substitution during admission.
    # Bash otherwise clears errexit there and can hide a failed/interrupted build.
    shopt -s inherit_errexit

    repo="''${AIRLOCK_MACHINE_REPO:-${repo}}"
    state="''${AIRLOCK_MACHINE_BUILD_STATE:-${state}}"
    store_root="''${AIRLOCK_CONFIG_NIX_STORE_ROOT:-${cfg.configNixStoreRoot}}"
    minimum_free_bytes=$((4 * 1024 * 1024 * 1024))

    usage() {
      printf '%s\n' \
        'usage: airlock-usr-assistant-machine-build preflight [--offline]' \
        '       airlock-usr-assistant-machine-build build [--offline] [.#INSTALLABLE]' \
        '       airlock-usr-assistant-machine-build admit [--offline] TARGET [.#INSTALLABLE]'
    }

    fail() {
      printf 'airlock-usr-assistant-machine-build: %s\n' "$*" >&2
      exit 1
    }

    check_repo() {
      [ -d "$repo/.git" ] || fail "missing Git checkout: $repo"
      [ -f "$repo/flake.lock" ] || fail "checkout is missing flake.lock"
      [ -z "$(${pkgs.gitMinimal}/bin/git -C "$repo" status --porcelain)" ] || fail 'checkout has local edits; commit or stash them before building'
    }

    free_bytes() {
      blocks="$(${pkgs.coreutils}/bin/stat -f -c '%a' "$store_root")"
      block_size="$(${pkgs.coreutils}/bin/stat -f -c '%S' "$store_root")"
      printf '%s\n' "$((blocks * block_size))"
    }

    offline_args=()
    load_offline_inputs() {
      local manifest=/run/airlock-builder/pool/.airlock-builder/flake-inputs.json
      [ -r "$manifest" ] || fail 'image has no pinned offline flake inputs'
      local expected_lock actual_lock
      expected_lock="$(${pkgs.jq}/bin/jq -er '.lock_hash' "$manifest")"
      actual_lock="$(${pkgs.coreutils}/bin/sha256sum "$repo/flake.lock" | ${pkgs.gawk}/bin/awk '{print $1}')"
      [ "$expected_lock" = "$actual_lock" ] || fail 'checkout flake.lock differs from the image offline inputs'
      [ "$(${pkgs.jq}/bin/jq -er '.schema' "$manifest")" = airlock.sys-agent-offline-inputs.v1 ] || fail 'invalid offline input manifest'
      local name path
      while IFS="$(printf '\t')" read -r name path; do
        case "$path" in
          /nix/store/*) ;;
          *) fail "invalid offline source path: $path" ;;
        esac
        [ -d "$store_root$path" ] || fail "missing pinned offline source: $name"
        offline_args+=(--override-input "$name" "path:$path")
      done < <(${pkgs.jq}/bin/jq -r '.inputs | to_entries[] | [.key, .value] | @tsv' "$manifest")
      [ "''${#offline_args[@]}" -gt 0 ] || fail 'offline input manifest is empty'
    }

    preflight() {
      check_repo
      ${pkgs.coreutils}/bin/install -d -m 0700 "$state" "$store_root"
      available="$(free_bytes)"
      [ "$available" -ge "$minimum_free_bytes" ] || fail "private Nix store needs at least 4 GiB free; only $available bytes are available"
      if [ "$offline_selected" = true ]; then
        load_offline_inputs
        airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes flake metadata --offline --no-write-lock-file "''${offline_args[@]}" "$repo" --json >/dev/null || fail 'pinned flake inputs cannot be evaluated offline'
        offline_ready=true
      else
        offline_ready=false
        if airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes flake metadata --offline --no-write-lock-file "$repo" --json >/dev/null 2>&1; then
          offline_ready=true
        fi
        airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes flake metadata --no-write-lock-file "$repo" --json >/dev/null || fail 'locked flake inputs are unavailable from the private store or configured network access'
      fi
      revision="$(${pkgs.gitMinimal}/bin/git -C "$repo" rev-parse HEAD)"
      lock_hash="$(${pkgs.coreutils}/bin/sha256sum "$repo/flake.lock" | ${pkgs.gawk}/bin/awk '{print $1}')"
      ${pkgs.jq}/bin/jq -n \
        --arg repo "$repo" \
        --arg revision "$revision" \
        --arg lockHash "$lock_hash" \
        --arg storeRoot "$store_root" \
        --argjson availableBytes "$available" \
        --argjson offlineReady "$offline_ready" \
        '{schema: "airlock.sys-agent-machine-build-preflight.v1", repo: $repo, revision: $revision, lock_hash: $lockHash, store_root: $storeRoot, available_bytes: $availableBytes, offline_inputs_ready: $offlineReady}'
    }

    build_installable() {
      installable="$1"
      case "$installable" in
        .#*) installable="''${installable#.}" ;;
        \#*) ;;
        *) fail 'installable must start with .# or #' ;;
    esac
      preflight >/dev/null
      if [ "$offline_selected" = true ]; then
        result="$(airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes build --offline --no-write-lock-file "''${offline_args[@]}" "$repo$installable" --no-link --json)"
      else
        result="$(airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes build --no-write-lock-file "$repo$installable" --no-link --json)"
      fi
      system="$(printf '%s\n' "$result" | ${pkgs.jq}/bin/jq -er '.[0].outputs.out')"
      printf '%s\n' "$system"
    }

    command="''${1:-}"
    [ -n "$command" ] || { usage >&2; exit 2; }
    shift
    offline_selected=false
    if [ "''${1:-}" = --offline ]; then
      offline_selected=true
      shift
    fi
    default_installable='.#nixosConfigurations.airlock-domain-usr-home.config.system.build.toplevel'
    case "$command" in
      preflight)
        [ "$#" -eq 0 ] || { usage >&2; exit 2; }
        preflight
        ;;
      build)
        [ "$#" -le 1 ] || { usage >&2; exit 2; }
        build_installable "''${1:-$default_installable}"
        ;;
      admit)
        [ "$#" -ge 1 ] && [ "$#" -le 2 ] || { usage >&2; exit 2; }
        target="$1"
        installable="''${2:-$default_installable}"
        printf '%s' "$target" | ${pkgs.gnugrep}/bin/grep -Eq '^[A-Za-z0-9_.-]+$' || fail 'invalid admission target'
        system="$(build_installable "$installable")"
        revision="$(${pkgs.gitMinimal}/bin/git -C "$repo" rev-parse HEAD)"
        lock_hash="$(${pkgs.coreutils}/bin/sha256sum "$repo/flake.lock" | ${pkgs.gawk}/bin/awk '{print $1}')"
        ${pkgs.coreutils}/bin/install -d -m 0700 "$state/closures" "$state/admissions"
        closure="$(${pkgs.coreutils}/bin/mktemp -d "$state/closures/$(${pkgs.coreutils}/bin/basename "$system").XXXXXX")"
        airlock-config-nix path-info --recursive --json "$system" |
          ${pkgs.jq}/bin/jq -r 'if type == "array" then .[] | .path else keys[] end' |
          ${pkgs.coreutils}/bin/sort -u > "$closure/store-paths"
        ${pkgs.gnugrep}/bin/grep -Fx "$system" "$closure/store-paths" >/dev/null || fail 'built system is absent from its closure metadata'
        response="$(${pkgs.coreutils}/bin/mktemp "$state/admissions/response.XXXXXX")"
        airlock-pool-send \
          --domain "$target" \
          --system "$system" \
          --closure-source "$closure" \
          --source-revision "$revision" \
          --lock-hash "$lock_hash" > "$response"
        ${pkgs.jq}/bin/jq -e '.status == "passed" and .activated == false and .retained == true' "$response" >/dev/null || fail 'pool admission did not create a retained inactive generation'
        recorded="$state/admissions/$(${pkgs.coreutils}/bin/date -u +%Y%m%dT%H%M%SZ)-$(${pkgs.coreutils}/bin/basename "$system").json"
        annotated="''${response}.annotated"
        ${pkgs.jq}/bin/jq --arg receiptPath "$recorded" '. + {receipt_path: $receiptPath}' "$response" > "$annotated"
        ${pkgs.coreutils}/bin/mv "$annotated" "$recorded"
        ${pkgs.coreutils}/bin/rm -f "$response"
        exec ${pkgs.coreutils}/bin/cat "$recorded"
        ;;
      *)
        usage >&2
        exit 2
        ;;
      esac
  '';
  configNixStore = pkgs.writeShellScriptBin "airlock-config-nix-store" ''
    set -eu

    ready=/run/airlock-builder/ready.json
    [ -r "$ready" ] \
      && ${pkgs.util-linux}/bin/mountpoint -q ${lib.escapeShellArg "${cfg.configNixStoreRoot}/nix/store"} \
      && ${pkgs.util-linux}/bin/mountpoint -q /run/airlock-builder/lower/nix/store || {
      echo '${role} pool-backed build store is not ready; inspect airlock-${role}-build-store.service' >&2
      exit 1
    }
    store_uri="$(${pkgs.jq}/bin/jq -er .uri "$ready")"
    exec ${pkgs.nix}/bin/nix-store \
      --option experimental-features 'nix-command flakes local-overlay-store read-only-local-store' \
      --option build-hook "" --store "$store_uri" "$@"
  '';
  airlockCli = pkgs.callPackage ../../runtime/tools/pkgs/airlock-cli {
    inherit configNixStore;
  };
  ctrlDeploy = pkgs.writeShellApplication {
    name = "airlock-sys-ctrl-guest-deploy";
    runtimeInputs = [
      sourceTool buildTool configNixStore
      pkgs.coreutils pkgs.gitMinimal pkgs.gnutar pkgs.gzip pkgs.gawk pkgs.jq
      pkgs.python3 pkgs.systemd pkgs.python3.pkgs.qubes-core-qrexec.domU
    ];
    text = ''
      exec ${pkgs.python3}/bin/python3 ${lib.fileset.toSource {
        root = ../../runtime/tools;
        fileset = lib.fileset.unions [
          ../../runtime/tools/sys-ctrl-guest-deploy.py
          ../../runtime/tools/sys_ctrl_review.py
          ../../runtime/tools/airlock_state.py
        ];
      }}/sys-ctrl-guest-deploy.py "$@"
    '';
  };
in
{
  environment.systemPackages = [
    sourceTool
    buildTool
    configNixStore
    ctrlDeploy
    pkgs.gitMinimal
    pkgs.gnutar
  ] ++ (if role == "usr-assistant" then [ airlockCli ] else [ ]);
  systemd.services.airlock-controller-observe = lib.mkIf (role == "sys-ctrl") {
    description = "Record authoritative dom0 deployment outcomes";
    after = [ "qubes-qrexec-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      User = cfg.user;
      ExecStart = "${ctrlDeploy}/bin/airlock-sys-ctrl-guest-deploy observe";
      TimeoutStartSec = 30;
    };
  };
  systemd.timers.airlock-controller-observe = lib.mkIf (role == "sys-ctrl") {
    wantedBy = [ "timers.target" ];
    timerConfig = { OnBootSec = "1min"; OnUnitInactiveSec = "1min"; };
  };
  environment.etc."qubes-rpc/airlock.Ctrl.GuestDeploy" = lib.mkIf (role == "sys-ctrl") {
    source = pkgs.writeShellScript "airlock-ctrl-guest-deploy-qrexec" ''
      # Old host policy still names sys-agent during the controller bootstrap.
      # Python accepts that caller only for the explicit rename wire protocol;
      # normal requests require usr-assistant. The host Qubes policy is unchanged.
      case "''${QREXEC_REMOTE_DOMAIN:-}" in
        usr-assistant|sys-agent) ;;
        *) exit 1 ;;
      esac
      exec ${ctrlDeploy}/bin/airlock-sys-ctrl-guest-deploy serve
    '';
    mode = "0555";
  };
  environment.etc."airlock/controller-review-required" = lib.mkIf (role == "sys-ctrl") { text = "v1\n"; };
  environment.etc."qubes/rpc-config/airlock.Ctrl.GuestDeploy" = lib.mkIf (role == "sys-ctrl") { text = "force-user='${cfg.user}'\n"; };
  environment.etc."qubes-rpc/airlock.Ctrl.ReviewStatus" = lib.mkIf (role == "sys-ctrl") {
    source = pkgs.writeShellScript "airlock-review-status-qrexec" ''
      exec ${ctrlDeploy}/bin/airlock-sys-ctrl-guest-deploy review-status-serve
    '';
    mode = "0555";
  };
  environment.etc."qubes/rpc-config/airlock.Ctrl.ReviewStatus" = lib.mkIf (role == "sys-ctrl") { text = "force-user='${cfg.user}'\n"; };
}
