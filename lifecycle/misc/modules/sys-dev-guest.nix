{ config, lib, pkgs, ... }@args:
let
  airlockMachineSeedArchive = args.airlockMachineSeedArchive or null;
in
{
  imports = [ ./sys-dev-machine-build.nix ];

  # sys-dev is an intentionally privileged USB-lab development domain. It
  # owns the NIC during the first networking stage and is the Tailnet entry
  # point for the development-only dom0 qrexec shell.
  services.tailscale = {
    enable = true;
    extraUpFlags = [ "--ssh" ];
  };
  environment.systemPackages = [ pkgs.jq ];
  environment.etc."qubes-rpc/airlock.DevTailscaleUp" = {
    source = pkgs.writeShellScript "airlock-dev-tailscale-up" ''
      set -eu
      [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-gui ] || exit 1
      [ -z "''${QREXEC_SERVICE_ARGUMENT:-}" ] || exit 1
      exec ${pkgs.tailscale}/bin/tailscale up --ssh
    '';
    mode = "0555";
  };
  environment.etc."qubes/rpc-config/airlock.DevTailscaleUp" = {
    text = "force-user='root'\n";
    mode = "0444";
  };
  # USB-lab's local dom0 operator needs to perform the one-time Tailnet
  # enrollment before remote access exists. This is intentionally confined to
  # the privileged development domain.
  airlock.qubeGuest.allowQrexecUserSelection = true;
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 22 ];
  airlock.devDom0Terminal.guest.enable = true;

  systemd.services.airlock-sys-dev-pool-init = {
    description = "Prepare sys-dev's writable Nix store for pool artifacts";
    wantedBy = [ "multi-user.target" ];
    wants = [ "airlock-qubes-rw-init.service" ];
    requires = [ "airlock-qubes-rw-init.service" ];
    after = [ "airlock-qubes-rw-init.service" ];
    before = [ "qubes-qrexec-agent.service" ];
    path = [ pkgs.coreutils ];
    script = ''
      install -d -m 0700 -o ${lib.escapeShellArg config.airlock.qubeGuest.user} -g users \
        ${lib.escapeShellArg config.airlock.qubeGuest.configNixStoreRoot}
    '';
    serviceConfig.Type = "oneshot";
  };

  systemd.services.airlock-sys-dev-machine-seed = lib.mkIf (airlockMachineSeedArchive != null) {
    description = "Seed the pinned shallow airlock-machine checkout in sys-dev";
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
      [ "$(jq -r '.schema // empty' "$temp/manifest.json")" = airlock.sys-dev-machine-seed.v1 ] || { echo 'unsupported airlock-machine seed' >&2; exit 1; }
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
