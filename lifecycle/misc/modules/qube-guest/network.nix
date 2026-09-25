{ config, lib, pkgs, qubesGuestPath, qubesPython, networkProviderPackages }:
let
  cfg = config.airlock.qubeGuest;
  hasNetworkDriverRole = cfg.role == "sys-net";
  hasNetworkProviderRole = hasNetworkDriverRole || cfg.role == "sys-firewall";
  qubesAgentLinux = pkgs.qubes-core-agent-linux;
  systemdPackage = config.systemd.package;
  qubesDnatToNs = pkgs.writeShellScript "airlock-qubes-setup-dnat-to-ns" ''
    exec ${qubesPython}/bin/python ${qubesAgentLinux}/lib/qubes/qubes-setup-dnat-to-ns "$@"
  '';
  qubesNetworkProxySetup = pkgs.runCommand "airlock-qubes-network-proxy-setup" { } ''
    install -D -m 0555 ${qubesAgentLinux}/lib/qubes/init/network-proxy-setup.sh "$out/bin/network-proxy-setup"
    substituteInPlace "$out/bin/network-proxy-setup" \
      --replace-fail "${qubesAgentLinux}/lib/qubes/qubes-setup-dnat-to-ns" "${qubesDnatToNs}"
  '';
  networkGuestPath = qubesGuestPath ++ networkProviderPackages;
  xenDriverDomainEtc = pkgs.symlinkJoin {
    name = "airlock-xen-driver-domain-etc";
    paths = [
      pkgs.qubes-vmm-xen
      qubesAgentLinux
      pkgs.qubes-linux-utils
    ];
    stripPrefix = "/etc/xen";
  };

  updatesProxyRpcService = pkgs.writeShellScript "airlock-qubes-updates-proxy-rpc-service" ''
    set -euo pipefail
    exec ${pkgs.socat}/bin/socat STDIO TCP:127.0.0.1:8082
  '';
  tinyproxyUpdatesConfig = pkgs.writeText "tinyproxy-updates.conf" ''
    User tinyproxy
    Group tinyproxy
    Port 8082
    Timeout 60
    DefaultErrorFile "${pkgs.tinyproxy}/share/tinyproxy/default.html"
    StatFile "${pkgs.tinyproxy}/share/tinyproxy/stats.html"
    Syslog On
    LogLevel Notice
    PidFile "/run/tinyproxy-updates/tinyproxy.pid"

    MaxClients 50
    DisableViaHeader Yes

    Allow 127.0.0.1
    Allow 10.137.0.0/16

    ConnectPort 443
    ConnectPort 873

    Filter "${qubesAgentLinux}/etc/tinyproxy/updates-blacklist"
  '';

  qubesSetupIp = pkgs.runCommand "airlock-qubes-setup-ip" { } ''
    install -D -m 0555 ${qubesAgentLinux.src}/network/setup-ip "$out/bin/setup-ip"
    substituteInPlace "$out/bin/setup-ip" \
      --replace-fail "/usr/lib/qubes/init/functions" "${qubesAgentLinux}/lib/qubes/init/functions" \
      --replace-fail "/usr/lib/qubes/qubes-setup-dnat-to-ns" "${qubesDnatToNs}" \
      --replace-fail "/usr/bin/qubesdb-read" "${pkgs.qubes-core-qubesdb}/bin/qubesdb-read" \
      --replace-fail "/usr/bin/resolvectl" "${systemdPackage}/bin/resolvectl" \
      --replace-fail "/usr/bin/nmcli" "${pkgs.networkmanager}/bin/nmcli" \
      --replace-fail "/lib/systemd/systemd-sysctl" "${systemdPackage}/lib/systemd/systemd-sysctl"
    patchShebangs "$out/bin/setup-ip"
  '';

  qubesIptablesStart = pkgs.writeShellScript "airlock-qubes-iptables-start" ''
    set -eu

    apply_rules() {
      family="$1"
      rules="${qubesAgentLinux}/etc/qubes/qubes-$family.nft"

      if [ "$family" = ipv6 ] &&
        ! qubesdb-read /qubes-ip6 >/dev/null 2>&1 &&
        ! qubesdb-read /qubes-netvm-gateway6 >/dev/null 2>&1; then
        rules="${qubesAgentLinux}/etc/qubes/qubes-ipv6-disabled.nft"
      fi

      [ -f "$rules" ] || return 6
      nft -f "$rules"
    }

    apply_rules ipv4
    apply_rules ipv6
  '';

  qubesNetworkManagerHook = pkgs.writeShellScript "airlock-qubes-nmhook" ''
    export PATH=${lib.escapeShellArg (lib.makeBinPath networkGuestPath)}:$PATH

    case "''${2:-}" in
      up|vpn-up|vpn-down|dhcp4-change)
        ${qubesDnatToNs}
        rc="$?"
        if [ "$rc" -ne 100 ]; then
          systemctl --no-block try-restart qubes-updates-proxy.service
        fi
        ;;
    esac

    exit 0
  '';

  qubesExternalIpHook = pkgs.writeShellScript "airlock-qubes-external-ip" ''
    case "''${2:-}" in
      up)
        inet="$(${pkgs.iproute2}/bin/ip addr show dev "$1" | ${pkgs.gnugrep}/bin/grep inet || true)"
        ${pkgs.qubes-core-qubesdb}/bin/qubesdb-write /qubes-netvm-external-ip "$inet"
        ;;
      down)
        ${pkgs.qubes-core-qubesdb}/bin/qubesdb-write /qubes-netvm-external-ip ""
        ;;
    esac
  '';

in
{
  config = lib.mkIf cfg.enable {
    # QubesDB supplies addressing for Xen frontend NICs; there is no DHCP
    # server on the link to the NetVM. Physical NICs use NetworkManager.
    networking.useDHCP = lib.mkDefault false;
    # setup-ip owns resolver configuration in guests with a Qubes uplink.
    # Running resolvconf too races its direct writes to /etc/resolv.conf.
    networking.resolvconf.enable = lib.mkIf (!hasNetworkDriverRole) false;
    hardware.enableRedistributableFirmware = lib.mkIf hasNetworkDriverRole true;
    networking.firewall.enable = lib.mkIf hasNetworkProviderRole false;
    networking.networkmanager.enable = lib.mkIf hasNetworkDriverRole true;
    networking.networkmanager.settings = lib.mkIf hasNetworkDriverRole {
      main.dns = "default";
      keyfile."unmanaged-devices" = "mac:fe:ff:ff:ff:ff:ff";
      device."wifi.scan-rand-mac-address" = "yes";
      connection = {
        "wifi.cloned-mac-address" = "stable";
        "connection.stable-id" = "\${CONNECTION}/\${BOOT}";
        "ipv6.ip6-privacy" = "2";
      };
    };

    users.groups.tinyproxy = lib.mkIf hasNetworkProviderRole { };
    users.users.tinyproxy = lib.mkIf hasNetworkProviderRole {
      isSystemUser = true;
      group = "tinyproxy";
    };

    systemd.services.airlock-network-driver-networkmanager-state = lib.mkIf hasNetworkDriverRole {
      description = "Prepare persistent NetworkManager state for the network driver domain";
      wants = [ "airlock-qubes-rw-init.service" ];
      requires = [ "airlock-qubes-rw-init.service" ];
      after = [ "airlock-qubes-rw-init.service" ];
      before = [ "NetworkManager.service" ];
      path = [
        pkgs.coreutils
      ];
      script = ''
        set -eu

        target=/rw/config/NM-system-connections
        link=/etc/NetworkManager/system-connections

        install -d -m 0700 "$target"
        install -d -m 0755 /etc/NetworkManager

        if [ -d "$link" ] && [ ! -L "$link" ]; then
          for entry in "$link"/*; do
            [ -e "$entry" ] || continue
            mv -n "$entry" "$target"/
          done
          rmdir "$link" 2>/dev/null || rm -rf "$link"
        elif [ -e "$link" ] && [ ! -L "$link" ]; then
          rm -rf "$link"
        fi

        ln -sfn "$target" "$link"
        chmod 0700 "$target"
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
    };

    systemd.services.NetworkManager = lib.mkIf hasNetworkDriverRole {
      wants = [
        "airlock-network-driver-networkmanager-state.service"
        "qubes-sysinit.service"
      ];
      requires = [ "airlock-network-driver-networkmanager-state.service" ];
      after = [
        "airlock-network-driver-networkmanager-state.service"
        "qubes-sysinit.service"
      ];
      unitConfig.ConditionPathExists = "/run/qubes-service/network-manager";
    };

    systemd.services.qubes-antispoof = lib.mkIf hasNetworkProviderRole {
      description = "Qubes anti-spoofing firewall rules";
      before = [ "network-pre.target" ];
      requiredBy = [ "network-pre.target" ];
      path = [ pkgs.nftables ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${pkgs.nftables}/bin/nft -f ${qubesAgentLinux}/etc/qubes/qubes-antispoof.nft";
      };
    };

    systemd.services.qubes-iptables = lib.mkIf hasNetworkProviderRole {
      description = "Qubes base firewall settings";
      requires = [ "qubes-antispoof.service" ];
      after = [ "qubes-antispoof.service" ];
      before = [ "network-pre.target" ];
      requiredBy = [ "network-pre.target" ];
      path = networkGuestPath;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${qubesIptablesStart}";
      };
    };

    systemd.services.qubes-network = lib.mkIf hasNetworkProviderRole {
      description = "Qubes network forwarding setup";
      wantedBy = [ "multi-user.target" ];
      wants = [
        "qubes-iptables.service"
        "qubes-sysinit.service"
      ];
      requires = [ "qubes-iptables.service" ];
      after = [
        "network-pre.target"
        "qubes-iptables.service"
        "qubes-sysinit.service"
      ];
      before = [ "network.target" ];
      path = networkGuestPath;
      unitConfig.ConditionPathExists = "/run/qubes-service/qubes-network";
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${qubesNetworkProxySetup}/bin/network-proxy-setup";
        ExecStop = "${qubesAgentLinux}/lib/qubes/init/network-proxy-stop.sh";
      };
    };

    systemd.services.qubes-firewall = lib.mkIf hasNetworkProviderRole {
      description = "Qubes firewall updater";
      wantedBy = [ "multi-user.target" ];
      wants = [ "qubes-iptables.service" ];
      after = [ "qubes-iptables.service" ];
      before = [ "qubes-network.service" ];
      path = networkGuestPath;
      unitConfig.ConditionPathExists = "/run/qubes-service/qubes-firewall";
      serviceConfig = {
        Type = "notify-reload";
        ExecStart = "${qubesAgentLinux}/bin/qubes-firewall";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    systemd.services.qubes-updates-proxy = lib.mkIf hasNetworkProviderRole {
      description = "Qubes updates proxy";
      wantedBy = [ "multi-user.target" ];
      after = [ "qubes-iptables.service" ];
      unitConfig.ConditionPathExists = "|/run/qubes-service/qubes-updates-proxy";
      serviceConfig = {
        ExecStart = "${pkgs.tinyproxy}/bin/tinyproxy -d -c ${tinyproxyUpdatesConfig}";
        Restart = "on-failure";
        RestartSec = "5s";
        RuntimeDirectory = "tinyproxy-updates";
      };
    };

    # Match Xen frontends, including interfaces named before the .link file is
    # available in the initrd. Backend vif devices belong to vif-route-qubes.
    # Qubes restarts the service explicitly on hotplug: SYSTEMD_WANTS can miss
    # an add event when udev renames the interface.
    services.udev.extraRules = ''
      SUBSYSTEM=="net", ACTION=="add", DRIVERS=="vif", KERNEL!="vif*", RUN+="${systemdPackage}/bin/systemctl restart --job-mode=replace qubes-network-uplink@%k.service"
      SUBSYSTEM=="net", ACTION=="remove", DRIVERS=="vif", KERNEL!="vif*", RUN+="${systemdPackage}/bin/systemctl stop --job-mode=replace qubes-network-uplink@%k.service"
    '';

    systemd.services."qubes-network-uplink@" = {
      description = "Qubes network uplink setup";
      after = [ "network-pre.target" "qubes-db.service" "qubes-sysinit.service" "sys-subsystem-net-devices-%i.device" ];
      requires = [ "network-pre.target" "qubes-db.service" "qubes-sysinit.service" ];
      bindsTo = [ "sys-subsystem-net-devices-%i.device" ];
      before = [ "network.target" ];
      wants = [ "network.target" ];
      path = qubesGuestPath ++ [ pkgs.iproute2 pkgs.networkmanager pkgs.nftables ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${qubesSetupIp}/bin/setup-ip add %i";
        ExecStop = "${qubesSetupIp}/bin/setup-ip remove %i";
      };
    };

    systemd.services.qubes-network-uplink = {
      description = "Configure Qubes uplinks already discovered in the initrd";
      wantedBy = [ "multi-user.target" ];
      requires = [ "qubes-db.service" "qubes-sysinit.service" ];
      wants = [ "systemd-udev-settle.service" ];
      after = [ "qubes-db.service" "qubes-sysinit.service" "systemd-udev-settle.service" ];
      path = [ pkgs.coreutils systemdPackage ];
      # The initrd may have processed the add event before the root's udev
      # rule was available. Reconcile existing frontends as well as hotplug.
      script = ''
        for interface in /sys/class/net/*; do
          case "$(basename "$interface")" in vif*) continue ;; esac
          [ -e "$interface/device/driver" ] || continue
          [ "$(basename "$(readlink -f "$interface/device/driver")")" = vif ] || continue
          systemctl start "qubes-network-uplink@$(basename "$interface").service"
        done
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
    };

    systemd.services.xendriverdomain = lib.mkIf hasNetworkProviderRole {
      enable = true;
      wantedBy = [ "multi-user.target" ];
      wants = [ "proc-xen.mount" ];
      requires = [ "proc-xen.mount" ];
      after = [
        "proc-xen.mount"
        "systemd-modules-load.service"
      ];
      path = networkGuestPath;
      overrideStrategy = "asDropin";
      serviceConfig = {
        LogsDirectory = "xen";
        RuntimeDirectory = "xen";
        RuntimeDirectoryMode = "0755";
      };
    };

    environment.etc."NetworkManager/dispatcher.d/qubes-nmhook" = lib.mkIf hasNetworkDriverRole {
      source = qubesNetworkManagerHook;
      mode = "0555";
    };

    environment.etc."NetworkManager/dispatcher.d/30-qubes-external-ip" = lib.mkIf hasNetworkDriverRole {
      source = qubesExternalIpHook;
      mode = "0555";
    };

    # Apply upstream's Xen frontend naming rule to every guest with a NetVM.
    # Keeping the name stable lets the hotplug unit address the same device.
    environment.etc."systemd/network/80-qubes-vif.link" = {
      source = "${qubesAgentLinux}/lib/systemd/network/80-qubes-vif.link";
      mode = "0444";
    };

    environment.etc."xen" = lib.mkIf hasNetworkProviderRole {
      source = xenDriverDomainEtc;
    };

    environment.etc."qubes/qubes-antispoof.nft" = lib.mkIf hasNetworkProviderRole {
      source = "${qubesAgentLinux}/etc/qubes/qubes-antispoof.nft";
      mode = "0444";
    };

    environment.etc."qubes/qubes-ipv4.nft" = lib.mkIf hasNetworkProviderRole {
      source = "${qubesAgentLinux}/etc/qubes/qubes-ipv4.nft";
      mode = "0444";
    };

    environment.etc."qubes/qubes-ipv6.nft" = lib.mkIf hasNetworkProviderRole {
      source = "${qubesAgentLinux}/etc/qubes/qubes-ipv6.nft";
      mode = "0444";
    };

    environment.etc."qubes/qubes-ipv6-disabled.nft" = lib.mkIf hasNetworkProviderRole {
      source = "${qubesAgentLinux}/etc/qubes/qubes-ipv6-disabled.nft";
      mode = "0444";
    };

    environment.etc."qubes-rpc/qubes.UpdatesProxy" = lib.mkIf hasNetworkProviderRole {
      source = updatesProxyRpcService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/qubes.UpdatesProxy" = lib.mkIf hasNetworkProviderRole {
      text = ''
        skip-service-descriptor=true
        exit-on-service-eof=true
      '';
      mode = "0444";
    };

  };
}
