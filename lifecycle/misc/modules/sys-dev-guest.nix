{ config, lib, pkgs, ... }:
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
}
