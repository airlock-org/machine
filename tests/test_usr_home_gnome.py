"""GNOME desktop-work applications run in usr-home, not sys-gui."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_usr_home_integrations_do_not_start_another_desktop_or_installer():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        home = f.nixosConfigurations.airlock-domain-usr-home.config;
      in {{
        installation = {{ nix = home.nix.enable; channels = home.nix.channel.enable; flatpak = home.services.flatpak.enable; installerTools = !home.system.disableInstallerTools; }};
        desktop = {{ gnome = home.services.desktopManager.gnome.enable; shell = home.services.gnome.core-shell.enable; gdm = home.services.displayManager.gdm.enable; }};
        integrations = {{
          mime = home.xdg.mime.enable;
          icons = home.xdg.icons.enable;
          autostart = home.xdg.autostart.enable;
          portal = home.xdg.portal.enable;
          keyring = home.services.gnome.gnome-keyring.enable;
          calendar = home.services.gnome.evolution-data-server.enable;
          gvfs = home.services.gvfs.enable;
          capture = home.airlock.captureTransfer.receive.enable;
          gui = home.systemd.services.qubes-gui-agent.enable;
          graphics = home.hardware.graphics.enable;
        }};
        portals = home.xdg.portal.config.common;
      }}
    """
    result = subprocess.run(
        [str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
         "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "usr-home-integrations"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "configuration.json").write_text(result.stdout)
    report = json.loads(result.stdout)
    assert not any(report["installation"].values())
    assert not any(report["desktop"].values())
    assert all(report["integrations"].values())
    assert report["portals"]["default"] == "gtk"


@pytest.mark.nix
@pytest.mark.parametrize(
    "profile, configuration",
    [
        (
            "usb-lab",
            "f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration",
        ),
        ("nested", "f.nixosConfigurations.vm-gui-config-dev.config"),
    ],
)
def test_personal_domains_explicitly_select_the_gui_vm(profile, configuration):
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        topology = ({configuration}).airlock.qubes.topology;
        runtime = import {ROOT}/lifecycle/bootstrap/initial-runtime-config/airlock/runtime.nix {{
          appliedMessage = "";
          system = "x86_64-linux";
        }};
      in {{
        defaultGuiVM = topology.prefs.default_guivm;
        personalGuiVMs = builtins.listToAttrs (map (name: {{
          inherit name;
          value = topology.domains.${{name}}.prefs.guivm or null;
        }}) [ "usr-home" ]);
        runtimeGuiVMs = builtins.listToAttrs (map (name: {{
          inherit name;
          value = runtime.nixNative.domains.${{name}}.prefs.guivm or null;
        }}) [ "usr-home" ]);
      }}
    """
    result = subprocess.run(
        [
            str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
            "eval", "--impure", "--json", "--expr", expression,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "personal-guivm"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{profile}.json").write_text(result.stdout)
    report = json.loads(result.stdout)
    assert report["defaultGuiVM"] == "sys-gui"
    # A global default cannot override a VM's existing explicit guivm=None.
    assert report["personalGuiVMs"] == {
        "usr-home": "sys-gui",
    }
    assert report["runtimeGuiVMs"] == report["personalGuiVMs"]


@pytest.mark.nix
def test_gnome_desktop_work_applications_are_routed_to_usr_home():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        workspace = (f.nixosConfigurations.airlock-domain-sys-gui-usb-gui-lab.extendModules {{
          modules = [ ({{ lib, ... }}: {{
            airlock.sysGuiGuest.defaultApplicationVM = lib.mkForce "usr-home";
          }}) ];
        }}).config;
        minimal = f.nixosConfigurations.airlock-domain-sys-gui-usb-gui-lab.config;
        appGuest = f.nixosConfigurations.airlock-domain-usr-home.config;
      in {{
        discovery = workspace.airlock.sysGuiGuest.applicationMenus.enable;
        workspaceCoreApps = workspace.services.gnome.core-apps.enable;
        minimalCoreApps = minimal.services.gnome.core-apps.enable;
        workspaceEtc = builtins.attrNames workspace.environment.etc;
        workspaceStartApp = workspace.environment.etc."qubes-rpc/qubes.StartApp".source;
        workspaceStartAppConfig = workspace.environment.etc."qubes/rpc-config/qubes.StartApp".text;
        launchHelper = builtins.readFile "${{builtins.head (builtins.filter
          (package: (package.name or "") == "airlock-launch-qube-app")
          workspace.environment.systemPackages)}}/bin/airlock-launch-qube-app";
        workspaceGSettings = workspace.services.desktopManager.gnome.extraGSettingsOverrides;
        guiLogRules = workspace.systemd.tmpfiles.rules;
        mutterFlags = workspace.nixpkgs.pkgs.mutter.mesonFlags;
        appGuestDhcp = appGuest.networking.useDHCP;
        appGuestUplink = {{ inherit (appGuest.systemd.services."qubes-network-uplink@") requires after bindsTo; }};
        appGuestForwarding = builtins.hasAttr "qubes-network" appGuest.systemd.services;
        appGuestUdev = appGuest.services.udev.extraRules;
        appGuestVifLink = toString appGuest.environment.etc."systemd/network/80-qubes-vif.link".source;
        appGui = map (cfg: {{
          enabled = cfg.systemd.services.qubes-gui-agent.enable;
          envPath = cfg.systemd.services.qubes-gui-agent.environment.ENV_PATH;
          restart = cfg.systemd.services.qubes-gui-agent.serviceConfig.Restart;
          restartSec = cfg.systemd.services.qubes-gui-agent.serviceConfig.RestartSec;
          startLimit = cfg.systemd.services.qubes-gui-agent.unitConfig.StartLimitIntervalSec;
          monitorLayout = builtins.readFile cfg.environment.etc."qubes-rpc/qubes.SetMonitorLayout".source;
          sessionReady = builtins.readFile cfg.environment.etc."qubes-rpc/airlock.AppSessionReady".source;
          sessionReadyConfig = cfg.environment.etc."qubes/rpc-config/airlock.AppSessionReady".text;
          forkAutostart = cfg.environment.etc."xdg/autostart/qubes-qrexec-fork-server.desktop".text;
          wantedBy = cfg.systemd.services.qubes-gui-agent.wantedBy;
          pam = cfg.security.pam.services.qubes-gui-agent.startSession;
          session = builtins.readFile cfg.environment.etc."X11/xinit/xinitrc".source;
          xorg = cfg.environment.etc."X11/xorg-qubes.conf.template".source;
          networkBoot = cfg.systemd.services.qubes-network-uplink.wantedBy;
          networkReconcile = cfg.systemd.services.qubes-network-uplink.script;
        }}) [ appGuest ];
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "usr-home-gnome"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "report.json").write_text(result.stdout)
    report = json.loads(result.stdout)
    assert "d /var/log/qubes 2775 root qubes - -" in report["guiLogRules"]
    assert any(
        flag.startswith("-Dxwayland_path=")
        and "xwayland-qubes" in flag
        and flag.endswith("/bin/Xwayland")
        for flag in report["mutterFlags"]
    )
    for guest in report["appGui"]:
        assert guest["envPath"] == "/run/current-system/sw/bin:/run/wrappers/bin"
        assert len(guest["envPath"]) < 250
        assert guest["restart"] == "on-failure"
        assert guest["restartSec"] == "5s"
        assert guest["startLimit"] == 0
        assert "export PATH=" in guest["monitorLayout"]
        assert "timeout --signal=TERM --kill-after=5s 60s" in guest["monitorLayout"]
        assert "/bin/qubes-set-monitor-layout" in guest["monitorLayout"]
        assert (
            "airlock.AppSessionReady is restricted to sys-gui" in guest["sessionReady"]
        )
        assert "/bin/xdpyinfo -display" in guest["sessionReady"]
        assert "systemctl --user show-environment" in guest["sessionReady"]
        assert "s/^DISPLAY=//p" in guest["sessionReady"]
        assert '"$XDG_RUNTIME_DIR/airlock-app-session-ready"' in guest["sessionReady"]
        assert guest["sessionReadyConfig"] == "force-user='user'\n"
        assert "/bin/qrexec-fork-server" in guest["forkAutostart"]
        assert "Exec=/usr/bin/" not in guest["forkAutostart"]
        assert guest["enabled"] and guest["pam"]
        assert "multi-user.target" in guest["wantedBy"]
        assert "rm -f" in guest["session"]
        assert "airlock-app-session-ready" in guest["session"]
        assert guest["xorg"]
        assert "multi-user.target" in guest["networkBoot"]
        assert "/sys/class/net/*" in guest["networkReconcile"]
    assert report["appGuestDhcp"] is False
    assert report["appGuestForwarding"] is False
    assert "qubes-db.service" in report["appGuestUplink"]["requires"]
    assert "qubes-sysinit.service" in report["appGuestUplink"]["after"]
    assert report["appGuestUplink"]["bindsTo"] == []
    assert "sys-subsystem-net-devices-%i.device" not in report["appGuestUplink"]["after"]
    assert 'DRIVERS=="vif"' in report["appGuestUdev"]
    assert "systemctl --no-block restart --job-mode=replace qubes-network-uplink@%k.service" in report["appGuestUdev"]
    assert "systemctl --no-block stop --job-mode=replace qubes-network-uplink@%k.service" in report["appGuestUdev"]
    assert 'KERNEL=="eth*"' not in report["appGuestUdev"]
    assert report["appGuestVifLink"].endswith("/80-qubes-vif.link")

    assert report["discovery"] is True
    assert report["workspaceCoreApps"] is False
    assert report["minimalCoreApps"] is True
    assert "xdg/autostart/airlock-gnome-desktop-shortcuts.desktop" not in report["workspaceEtc"]
    assert "airlock-qubes-start-app" in report["workspaceStartApp"]
    assert "force-user=" in report["workspaceStartAppConfig"]
    assert "airlock.AppSessionReady" in report["launchHelper"]
    assert "--auto --gui --pass-io --service" in report["launchHelper"]
    assert "qubes.WaitForSession" in report["launchHelper"]
    assert "supported-rpc.airlock.AppSessionReady" in report["launchHelper"]
    assert "readiness_deadline=$((SECONDS + 90))" in report["launchHelper"]
    assert '--foreground --signal=TERM --kill-after=5s "$remaining"' in report["launchHelper"]
    assert "notify-send" in report["launchHelper"]
    assert "ding@rastersoft.com" not in report["workspaceGSettings"]
