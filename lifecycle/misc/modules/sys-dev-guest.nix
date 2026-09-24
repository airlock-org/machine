{ pkgs, ... }:
{
  imports = [ ./sys-dev-machine-build.nix ./sys-dev-build-store.nix ./machine-source-seed.nix ];

  # sys-dev is an intentionally privileged USB-lab development domain. It
  # remains the Tailnet entry point for the development-only dom0 qrexec shell.
  services.tailscale = {
    enable = true;
    extraUpFlags = [ "--ssh" ];
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
