{ flake }:
let
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
  guest = flake.nixosConfigurations.airlock-domain-sys-agent.config;
  uplink = guest.systemd.services."qubes-network-uplink@";
  # Dummy interfaces exercise real udev/systemd device events without Xen.
  # Only the hardware matcher and setup-ip payload are substituted. Keep the
  # production commands and service dependency graph, including ordering and readiness prerequisites.
  rules = pkgs.lib.replaceStrings [ ''DRIVERS=="vif"'' ] [ ''KERNEL=="altest0"'' ]
    guest.services.udev.extraRules;
  blockingRules = pkgs.lib.replaceStrings
    [ ''DRIVERS=="vif"'' "qubes-network-uplink@" "--no-block " ]
    [ ''KERNEL=="alblock0"'' "airlock-blocking-uplink@" "" ]
    guest.services.udev.extraRules;
  service = {
    inherit (uplink) after requires bindsTo before wants;
    serviceConfig = uplink.serviceConfig // {
      ExecStart = [
        "${pkgs.iproute2}/bin/ip address add 192.0.2.2/32 dev %i"
        "${pkgs.iproute2}/bin/ip link set dev %i up"
      ];
      ExecStop = "-${pkgs.iproute2}/bin/ip address flush dev %i";
    };
  };
in
pkgs.testers.runNixOSTest {
  name = "airlock-uplink-hotplug";
  nodes.machine = {
    networking.useDHCP = false;
    services.udev.extraRules = rules + "\n" + blockingRules;
    systemd.services = {
      "qubes-network-uplink@" = service;
      "airlock-blocking-uplink@" = service // {
        after = service.after ++ [ "sys-subsystem-net-devices-%i.device" ];
        bindsTo = [ "sys-subsystem-net-devices-%i.device" ];
      };
      # QubesDB and sysinit availability are prerequisites, not the behavior
      # under test. No Qubes networking or authority runs on the test host.
      qubes-db.serviceConfig = { Type = "oneshot"; RemainAfterExit = true; ExecStart = "${pkgs.coreutils}/bin/true"; };
      qubes-sysinit.serviceConfig = { Type = "oneshot"; RemainAfterExit = true; ExecStart = "${pkgs.coreutils}/bin/true"; };
    };
    environment.systemPackages = [
      pkgs.iproute2
      (pkgs.python3.withPackages (ps: [ ps.pytest ]))
    ];
    environment.etc."uplink_hotplug.py".source = ./uplink_hotplug.py;
    virtualisation.memorySize = 768;
  };
  testScript = ''
    from datetime import timedelta

    machine.start()
    machine.wait_for_unit("multi-user.target")
    try:
        machine.succeed("pytest -v /etc/uplink_hotplug.py --junitxml=/tmp/uplink-hotplug.xml", timeout=timedelta(seconds=90))
    finally:
        machine.succeed("journalctl -b --no-pager > /tmp/uplink-journal.txt")
        machine.copy_from_machine("/tmp/uplink-journal.txt")
        machine.copy_from_machine("/tmp/uplink-hotplug.xml")
  '';
}
