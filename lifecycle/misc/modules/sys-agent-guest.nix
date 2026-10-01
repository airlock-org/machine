{ pkgs, ... }:
{
  imports = [ ./sys-agent-machine-build.nix ./sys-agent-build-store.nix ./machine-source-seed.nix ];

  # sys-agent is an intentionally privileged USB-lab development domain. It
  # remains the Tailnet entry point for the development-only dom0 qrexec shell.
  services.tailscale = {
    enable = true;
    extraUpFlags = [ "--ssh" ];
  };
  # Keep enrollment on Qubes' private volume across immutable root updates
  # and explicit VM replacements. Both services wait for private storage.
  systemd.services.airlock-agent-tailscale-state = {
    wantedBy = [ "multi-user.target" ];
    requires = [ "airlock-qubes-rw-init.service" ];
    after = [ "airlock-qubes-rw-init.service" ];
    before = [ "tailscaled.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.coreutils}/bin/install -d -m 0700 /rw/config/tailscale
    '';
  };
  systemd.services.tailscaled = {
    requires = [ "airlock-agent-tailscale-state.service" ];
    after = [ "airlock-agent-tailscale-state.service" ];
    serviceConfig.BindPaths = [ "/rw/config/tailscale:/var/lib/tailscale" ];
    serviceConfig.ExecStartPost = [ "${pkgs.tailscale}/bin/tailscale set --hostname=sys-agent" ];
  };
  environment.systemPackages = [ pkgs.jq ];
  environment.etc."qubes-rpc/airlock.DevTailscaleUp" = {
    source = pkgs.writeShellScript "airlock-dev-tailscale-up" ''
      export PATH=${pkgs.lib.makeBinPath [ pkgs.coreutils pkgs.glibc.bin pkgs.iproute2 pkgs.curl pkgs.tailscale ]}:$PATH
      ${builtins.readFile ../tools/scripts/dev-tailscale-up.sh}
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
}
