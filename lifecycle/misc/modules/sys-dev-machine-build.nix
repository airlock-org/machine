{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.qubeGuest;
  repo = "/home/${cfg.user}/dev/airlock-machine";
  state = "/home/${cfg.user}/.local/share/airlock/machine-build";
  cache = "/home/${cfg.user}/.cache/airlock-machine-source";
  sourceTool = pkgs.writeShellScriptBin "airlock-sys-dev-machine-source" ''
    set -eu

    usage() {
      printf '%s\n' \
        'usage: airlock-sys-dev-machine-source import --revision REV --lock-hash HASH < source.tar.gz' \
        '       airlock-sys-dev-machine-source status'
    }

    fail() {
      printf 'airlock-sys-dev-machine-source: %s\n' "$*" >&2
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
            '{schema: "airlock.sys-dev-machine-source-status.v1", present: true, repo: $repo, revision: $revision, lock_hash: $lockHash, seed_revision: $seedRevision, seed_lock_hash: $seedLockHash, dirty_paths: $dirty}'
        else
          ${pkgs.jq}/bin/jq -n --arg repo ${lib.escapeShellArg repo} \
            '{schema: "airlock.sys-dev-machine-source-status.v1", present: false, repo: $repo}'
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
        ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg cache} ${lib.escapeShellArg state} ${lib.escapeShellArg "/home/${cfg.user}/dev"}
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
        [ "$(${pkgs.jq}/bin/jq -r '.schema // empty' "$temp/manifest.json")" = 'airlock.sys-dev-machine-source.v1' ] || fail 'unsupported source archive schema'
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
  buildTool = pkgs.writeShellScriptBin "airlock-sys-dev-machine-build" ''
    set -eu

    repo="''${AIRLOCK_MACHINE_REPO:-${repo}}"
    state="''${AIRLOCK_MACHINE_BUILD_STATE:-${state}}"
    store_root="''${AIRLOCK_CONFIG_NIX_STORE_ROOT:-${cfg.configNixStoreRoot}}"
    minimum_free_bytes=$((4 * 1024 * 1024 * 1024))

    usage() {
      printf '%s\n' \
        'usage: airlock-sys-dev-machine-build preflight' \
        '       airlock-sys-dev-machine-build build [.#INSTALLABLE]' \
        '       airlock-sys-dev-machine-build admit TARGET [.#INSTALLABLE]'
    }

    fail() {
      printf 'airlock-sys-dev-machine-build: %s\n' "$*" >&2
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

    preflight() {
      check_repo
      ${pkgs.coreutils}/bin/install -d -m 0700 "$state" "$store_root"
      available="$(free_bytes)"
      [ "$available" -ge "$minimum_free_bytes" ] || fail "private Nix store needs at least 4 GiB free; only $available bytes are available"
      offline_ready=false
      if airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes flake metadata --offline "$repo" --json >/dev/null 2>&1; then
        offline_ready=true
      fi
      airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes flake metadata "$repo" --json >/dev/null || fail 'locked flake inputs are unavailable from the private store or configured network access'
      revision="$(${pkgs.gitMinimal}/bin/git -C "$repo" rev-parse HEAD)"
      lock_hash="$(${pkgs.coreutils}/bin/sha256sum "$repo/flake.lock" | ${pkgs.gawk}/bin/awk '{print $1}')"
      ${pkgs.jq}/bin/jq -n \
        --arg repo "$repo" \
        --arg revision "$revision" \
        --arg lockHash "$lock_hash" \
        --arg storeRoot "$store_root" \
        --argjson availableBytes "$available" \
        --argjson offlineReady "$offline_ready" \
        '{schema: "airlock.sys-dev-machine-build-preflight.v1", repo: $repo, revision: $revision, lock_hash: $lockHash, store_root: $storeRoot, available_bytes: $availableBytes, offline_inputs_ready: $offlineReady}'
    }

    build_installable() {
      installable="$1"
      case "$installable" in
        .#*) installable="''${installable#.}" ;;
        \#*) ;;
        *) fail 'installable must start with .# or #' ;;
      esac
      preflight >/dev/null
      result="$(airlock-config-nix --extra-experimental-features nix-command --extra-experimental-features flakes build "$repo$installable" --no-link --json)"
      system="$(printf '%s\n' "$result" | ${pkgs.jq}/bin/jq -er '.[0].outputs.out')"
      printf '%s\n' "$system"
    }

    command="''${1:-}"
    [ -n "$command" ] || { usage >&2; exit 2; }
    shift
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
        ${pkgs.coreutils}/bin/mv "$response" "$recorded"
        exec ${pkgs.coreutils}/bin/cat "$recorded"
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
  '';
in
{
  environment.systemPackages = [
    sourceTool
    buildTool
    pkgs.gitMinimal
    pkgs.gnutar
  ];
}
