{
  cfg,
  lib,
  pkgs,
  configInputsPublicPath,
  configStagingInit,
  hasConfigStagingRole,
  roleInitPathFor,
}:
rec {
  configInputsImportService = pkgs.writeShellScript "airlock-config-inputs-import-qrexec" ''
    set -eu

    export PATH=${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.gawk
        pkgs.gnugrep
        pkgs.gnutar
        pkgs.gzip
        pkgs.jq
        pkgs.nix
        pkgs.util-linux
      ]
    }

    import_user=${lib.escapeShellArg cfg.user}
    target_root="''${AIRLOCK_CONFIG_INPUTS_PATH:-${cfg.configInputsPath}}"
    store_root="''${AIRLOCK_CONFIG_NIX_STORE_ROOT:-${cfg.configNixStoreRoot}}"
    public_root=${lib.escapeShellArg configInputsPublicPath}
    case "$target_root" in
      /rw/*) ;;
      *)
        printf 'AIRLOCK_CONFIG_INPUTS_PATH must live under /rw: %s\n' "$target_root" >&2
        exit 1
        ;;
    esac
    case "$store_root" in
      /*) ;;
      *)
        printf 'AIRLOCK_CONFIG_NIX_STORE_ROOT must be absolute: %s\n' "$store_root" >&2
        exit 1
        ;;
    esac

    config_inputs_parent="''${target_root%/*}"
    mkdir -p "$config_inputs_parent" "$store_root" "$target_root"
    chown "$import_user:users" "$target_root" "$store_root" || true
    tmpdir="$(mktemp -d "$config_inputs_parent/.config-inputs-import.XXXXXX")"
    trap 'rm -rf "$tmpdir"' EXIT

    run_as_import_user() {
      ${pkgs.util-linux}/bin/runuser -u "$import_user" -- \
        ${pkgs.coreutils}/bin/env \
          HOME="/home/$import_user" \
          USER="$import_user" \
          LOGNAME="$import_user" \
          XDG_CACHE_HOME="/home/$import_user/.cache" \
          "$@"
    }

    mount_config_inputs() {
      mkdir -p "$public_root"
      jq -r '.inputs | to_entries[] | [.key, .value] | @tsv' "$target_root/manifest.json" |
        while IFS="$(printf '\t')" read -r name input_path; do
          case "$name" in
            nixpkgs|nixpkgs-libvirt_10_5|nixos-qubes|airlock-support) ;;
            *)
              printf 'invalid config input name: %s\n' "$name" >&2
              exit 1
              ;;
          esac
          input_store_path="$(printf '%s\n' "$input_path" | ${pkgs.gnused}/bin/sed 's|^\(/nix/store/[^/]*\).*|\1|')"
          input_relative_path="''${input_path#"$input_store_path"}"
          physical_path="$store_root$input_store_path$input_relative_path"
          [ -d "$physical_path" ] || {
            printf 'imported config input path is missing from private Nix store: %s\n' "$physical_path" >&2
            exit 1
          }
          mountpoint_path="$public_root/$name"
          if mountpoint -q "$mountpoint_path"; then
            umount "$mountpoint_path"
          fi
          if [ -e "$mountpoint_path" ] && [ ! -d "$mountpoint_path" ]; then
            rm -rf "$mountpoint_path"
          fi
          mkdir -p "$mountpoint_path"
          mount --bind "$physical_path" "$mountpoint_path"
        done
    }

    archive="$tmpdir/config-inputs.tar"
    cat > "$archive"
    mkdir -p "$tmpdir/unpack"

    tar -tf "$archive" |
      awk '
        $0 == "" ||
        $0 ~ /^\// ||
        $0 ~ /(^|\/)\.\.($|\/)/ ||
        $0 ~ /\/\// {
          printf "unsafe config input tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
        $0 == "manifest.json" ||
        $0 == "store-paths" ||
        $0 == "store-export.nar.gz" {
          next
        }
        {
          printf "unexpected config input tar member: %s\n", $0 > "/dev/stderr"
          exit 1
        }
      '

    tar -C "$tmpdir/unpack" -xf "$archive"
    [ -f "$tmpdir/unpack/manifest.json" ] || {
      printf 'config input tar is missing manifest.json\n' >&2
      exit 1
    }
    [ -f "$tmpdir/unpack/store-paths" ] || {
      printf 'config input tar is missing store-paths\n' >&2
      exit 1
    }
    [ -f "$tmpdir/unpack/store-export.nar.gz" ] || {
      printf 'config input tar is missing store-export.nar.gz\n' >&2
      exit 1
    }

    jq -e '
      .schema == "airlock.config-inputs.v1" and
      (.inputs | keys | sort) == [
        "airlock-support",
        "nixos-qubes",
        "nixpkgs",
        "nixpkgs-libvirt_10_5"
      ] and
      all(.inputs[];
        test("^/nix/store/[A-Za-z0-9]+-[^/]+($|/)") and
        (contains("//") | not) and
        (contains("/../") | not)
      )
    ' "$tmpdir/unpack/manifest.json" >/dev/null

    awk '
      $0 !~ /^\/nix\/store\/[A-Za-z0-9]+-[^\/]+$/ {
        printf "invalid config input store path: %s\n", $0 > "/dev/stderr"
        exit 1
      }
    ' "$tmpdir/unpack/store-paths"

    while IFS= read -r store_path; do
      if ! run_as_import_user nix-store --store "$store_root" --check-validity "$store_path" >/dev/null 2>&1; then
        rm -rf "$store_root$store_path" "$store_root$store_path.lock"
      fi
    done < "$tmpdir/unpack/store-paths"
    gzip -dc "$tmpdir/unpack/store-export.nar.gz" |
      run_as_import_user nix-store --store "$store_root" --import >/dev/null
    while IFS= read -r store_path; do
      run_as_import_user nix-store --store "$store_root" --check-validity "$store_path" >/dev/null
    done < "$tmpdir/unpack/store-paths"

    cp "$tmpdir/unpack/manifest.json" "$target_root/manifest.json.new"
    chown "$import_user:users" "$target_root/manifest.json.new" || true
    mv "$target_root/manifest.json.new" "$target_root/manifest.json"
    mount_config_inputs

    locked=false
    ${lib.optionalString hasConfigStagingRole ''
      run_staging_init() {
        ${pkgs.coreutils}/bin/env \
          HOME=/home/${lib.escapeShellArg cfg.user} \
          USER=${lib.escapeShellArg cfg.user} \
          LOGNAME=${lib.escapeShellArg cfg.user} \
          XDG_CACHE_HOME=/home/${lib.escapeShellArg cfg.user}/.cache \
          AIRLOCK_CONFIG_INPUTS_PATH=${lib.escapeShellArg configInputsPublicPath} \
          AIRLOCK_CONFIG_REFRESH_INPUTS=1 \
          PATH=${lib.escapeShellArg (roleInitPathFor "sys-config-staging")} \
          ${configStagingInit}/bin/airlock-config-staging-init
      }
      if [ "$(${pkgs.coreutils}/bin/id -un)" = ${lib.escapeShellArg cfg.user} ]; then
        run_staging_init
      else
        ${pkgs.util-linux}/bin/runuser -u ${lib.escapeShellArg cfg.user} -- \
          ${pkgs.coreutils}/bin/env \
            HOME=/home/${lib.escapeShellArg cfg.user} \
            USER=${lib.escapeShellArg cfg.user} \
            LOGNAME=${lib.escapeShellArg cfg.user} \
            XDG_CACHE_HOME=/home/${lib.escapeShellArg cfg.user}/.cache \
            AIRLOCK_CONFIG_INPUTS_PATH=${lib.escapeShellArg configInputsPublicPath} \
            AIRLOCK_CONFIG_REFRESH_INPUTS=1 \
            PATH=${lib.escapeShellArg (roleInitPathFor "sys-config-staging")} \
            ${configStagingInit}/bin/airlock-config-staging-init
      fi
      [ -f ${lib.escapeShellArg cfg.configRepoPath}/flake.lock ] && locked=true
    ''}

    printf '{"schema":"airlock.config-inputs-import.v1","status":"passed","path":"%s","flake_lock":%s}\n' \
      "$target_root" \
      "$locked"
  '';
}
