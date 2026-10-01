{
  cfg,
  lib,
  pkgs,
}:
rec {
  configNixWrapper = pkgs.writeShellScript "airlock-config-nix" ''
    set -eu

    for arg in "$@"; do
      case "$arg" in
        --store|--store=*)
          exec ${pkgs.nix}/bin/nix "$@"
          ;;
      esac
    done

    ${lib.optionalString (builtins.elem cfg.role [ "sys-agent" "sys-ctrl" ]) ''
      [ "''${AIRLOCK_CONFIG_NIX_STORE_ROOT:-${cfg.configNixStoreRoot}}" = ${lib.escapeShellArg cfg.configNixStoreRoot} ] || {
        echo '${cfg.role} uses its pinned pool-backed store; use an explicit --store for a different store' >&2
        exit 1
      }
      [ -r /run/airlock-builder/ready.json ] \
        && ${pkgs.util-linux}/bin/mountpoint -q ${lib.escapeShellArg "${cfg.configNixStoreRoot}/nix/store"} \
        && ${pkgs.util-linux}/bin/mountpoint -q /run/airlock-builder/lower/nix/store || {
        echo '${cfg.role} pool-backed build store is not ready; inspect airlock-${cfg.role}-build-store.service' >&2
        exit 1
      }
      store_uri="$(${pkgs.jq}/bin/jq -er .uri /run/airlock-builder/ready.json)"
      exec ${pkgs.nix}/bin/nix \
        --extra-experimental-features 'nix-command flakes local-overlay-store read-only-local-store' \
        --option build-hook "" --store "$store_uri" "$@"
    ''}

    store_root="''${AIRLOCK_CONFIG_NIX_STORE_ROOT:-${cfg.configNixStoreRoot}}"
    case "$store_root" in
      /*) ;;
      *)
        printf 'AIRLOCK_CONFIG_NIX_STORE_ROOT must be absolute: %s\n' "$store_root" >&2
        exit 1
        ;;
    esac

    mkdir -p "$store_root"
    exec ${pkgs.nix}/bin/nix --store "$store_root" "$@"
  '';
  configNix = pkgs.runCommand "airlock-config-nix" { } ''
    mkdir -p "$out/bin"
    ln -s ${configNixWrapper} "$out/bin/airlock-config-nix"
    ln -s ${configNixWrapper} "$out/bin/nix"
  '';
}
