{ config, lib, pkgs, ... }@args:
let
  airlockMachineSeedArchive = args.airlockMachineSeedArchive or null;
  offlineInputs = args.airlockMachineOfflineInputs or null;
in
{
  # Flake path inputs must also be visible at their logical /nix/store paths.
  # Nix's path fetcher reads that filesystem path even with an overlay store.
  system.extraDependencies = lib.optionals (offlineInputs != null) (
    map builtins.storePath (lib.unique (lib.attrValues offlineInputs.inputs))
  );

  systemd.services.airlock-machine-source-seed = lib.mkIf (airlockMachineSeedArchive != null) {
    description = "Seed the pinned shallow airlock-machine checkout";
    wantedBy = [ "multi-user.target" ];
    wants = [ "airlock-qubes-rw-init.service" ];
    requires = [ "airlock-qubes-rw-init.service" ];
    after = [ "airlock-qubes-rw-init.service" ];
    before = [ "qubes-qrexec-agent.service" ];
    path = [ pkgs.coreutils pkgs.gitMinimal pkgs.gnugrep pkgs.gnutar pkgs.gzip pkgs.gawk pkgs.jq ];
    script = ''
      set -eu
      repo=/home/${config.airlock.qubeGuest.user}/dev/airlock-machine
      [ ! -e "$repo" ] || exit 0
      cache=/home/${config.airlock.qubeGuest.user}/.cache/airlock-machine-source
      install -d -m 0700 -o ${config.airlock.qubeGuest.user} -g users "$(dirname "$cache")" "$cache" "$(dirname "$repo")"
      temp="$(mktemp -d "$cache/seed.XXXXXX")"
      trap 'rm -rf "$temp"' EXIT
      tar -xzf ${lib.escapeShellArg airlockMachineSeedArchive} -C "$temp" --no-same-owner --no-same-permissions
      [ -d "$temp/repo/.git" ] && [ -f "$temp/manifest.json" ] || { echo 'invalid airlock-machine seed' >&2; exit 1; }
      [ "$(jq -r '.schema // empty' "$temp/manifest.json")" = airlock.sys-agent-machine-seed.v1 ] || { echo 'unsupported airlock-machine seed' >&2; exit 1; }
      revision="$(jq -r '.revision // empty' "$temp/manifest.json")"
      lock_hash="$(jq -r '.lock_hash // empty' "$temp/manifest.json")"
      [ "$(git -C "$temp/repo" rev-parse HEAD)" = "$revision" ]
      [ "$(git -C "$temp/repo" rev-parse --is-shallow-repository)" = true ]
      [ "$(sha256sum "$temp/repo/flake.lock" | awk '{print $1}')" = "$lock_hash" ]
      git -C "$temp/repo" config airlock.seedRevision "$revision"
      git -C "$temp/repo" config airlock.seedLockHash "$lock_hash"
      chown -R ${config.airlock.qubeGuest.user}:users "$temp/repo"
      mv "$temp/repo" "$repo"
    '';
    serviceConfig.Type = "oneshot";
  };
}
