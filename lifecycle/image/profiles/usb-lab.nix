{ lib, pkgs, ... }:
{
  imports = [ ./usb-gui-lab.nix ];

  # sys-agent remains the intentionally privileged Tailnet entry point.
  airlock.devDom0Terminal = {
    host.enable = true;
    callers = lib.mkForce [ "sys-agent" ];
  };
  airlock.poolAdmission = {
    enable = true;
    caller = "sys-ctrl";
    targets = [
      "sys-gui"
      "sys-agent"
      "sys-ctrl"
      "sys-net"
      "sys-firewall"
      "usr-home"
    ];
  };
  networking.networkmanager.enable = lib.mkForce false;
  services.tailscale.enable = lib.mkForce false;
  services.openssh.enable = lib.mkForce false;

  virtualisation.qubes.packages = [
    (pkgs.writeTextDir "etc/qubes/policy.d/40-airlock-dev-tailscale.policy" ''
      airlock.DevTailscaleUp + sys-gui sys-agent allow user=root
      airlock.DevTailscaleUp * @anyvm @anyvm deny
    '')
  ];

  airlock.qubes = {
    startNetworkProvidersOnBoot.enable = lib.mkForce true;
    prefs.default_netvm = lib.mkForce "sys-firewall";
  };

  environment.etc."airlock/usb-lab".text = ''
    experimental general USB lab
    sys-net owns physical networking; sys-agent provides Tailnet development access through sys-firewall
    run: tailscale ssh user@sys-agent; then airlock-dom0-shell
  '';
  environment.etc."airlock/boot-role".text = "normal\n";
}
