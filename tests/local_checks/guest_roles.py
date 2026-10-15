from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from .support import _physical_store_path, _run


def check_personal_domains(repo_root: Path, log_dir: Path) -> None:
    """Check personal-domain routing and persistent storage in the topology."""
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)

    topology_json = log_dir / "topology.json"
    _run(
        [
            "nix",
            "eval",
            "--json",
            f"{repo_root}#nixosConfigurations.vm-gui-config-dev.config.airlock.qubes.topology.domains",
        ],
        stdout=topology_json,
    )
    domains = json.loads(topology_json.read_text(encoding="utf-8"))
    home = domains["usr-home"]
    assert "usr-browser" not in domains

    assert home["netvm"] == "sys-firewall"
    assert home["prefs"]["autostart"] is True
    assert home["backend"]["state"]["persistent"] == "usr-home-private"
    assert home["features"]["menu-items"] == "*"


def check_cmd_role(repo_root: Path, log_dir: Path) -> None:
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    role_json = log_dir / "role.json"

    expr = f"""
    let
      flake = builtins.getFlake "path:{repo_root}";
      lib = flake.inputs.nixpkgs.lib;
      dom0Cfg = flake.nixosConfigurations."vm-gui-config-dev".config;
      releaseDom0Cfg = flake.nixosConfigurations."vm-gui-config-release".config;
      nonDevDom0Cfg = flake.nixosConfigurations.test.config;
      builders = import {repo_root}/lifecycle/misc/lib/qube-template.nix {{
        nixpkgs = flake.inputs.nixpkgs;
        nixpkgsLibvirt105Source = "${{flake.inputs.nixpkgs-libvirt_10_5}}";
        nixosQubesSource = "${{flake.inputs.nixos-qubes}}";
        airlockSupport = "{repo_root}";
        system = "x86_64-linux";
      }};
      defaultRuntime = import {repo_root}/lifecycle/bootstrap/initial-runtime-config/airlock/runtime.nix {{
        appliedMessage = "test";
        pkgs = builders.pkgs;
        system = "x86_64-linux";
        templateBuilders = builders;
      }};
      devEntry = builders.mkNixNativeDomain {{
        name = "sample-cmd";
        guestRole = "cmd";
        modules = [
          ({{ ... }}: {{
            networking.hostName = "sample-cmd";
          }})
        ];
        features = {{
          "airlock-role" = "cmd";
          "airlock-cmd" = "1";
        }};
      }};
      devCfg = devEntry.configuration.config;
      generatedDevDomain = devEntry.domain;
      sysNetDomainDefinition = defaultRuntime.nixNative.domains.sys-net;
      sysFirewallDomainDefinition = defaultRuntime.nixNative.domains.sys-firewall;
      sysGuiDomainDefinition = defaultRuntime.nixNative.domains.sys-gui;
      sysGuiCfg = flake.nixosConfigurations.airlock-domain-sys-gui.config;
      stagedNixNativeDomains = defaultRuntime.nixNative.domains;
      bootstrapDomains = dom0Cfg.airlock.qubes.domains;
      releaseBootstrapDomains = releaseDom0Cfg.airlock.qubes.domains;
      bootstrapSysNetDomain = bootstrapDomains.sys-net;
      bootstrapSysFirewallDomain = bootstrapDomains.sys-firewall;
      bootstrapSysGuiDomain = bootstrapDomains.sys-gui;
      bootstrapTopologySysNetDomain = dom0Cfg.airlock.qubes.topology.domains.sys-net;
      bootstrapTopologySysFirewallDomain =
        dom0Cfg.airlock.qubes.topology.domains.sys-firewall;
      baseCfg = (builders.mkNixNativeDomain {{
        name = "minimal-base";
        guestRole = "base";
        modules = [ ];
      }}).configuration.config;
      packageName = package: package.pname or package.name or "";
      dom0QubesPackages = map packageName dom0Cfg.virtualisation.qubes.packages;
      dom0SysGuiPolicyPackage = lib.findFirst (
        package: packageName package == "airlock-sys-gui-policy"
      ) (throw "missing Airlock sys-gui policy package") dom0Cfg.virtualisation.qubes.packages;
      dom0SysGuiPolicy = builtins.readFile "${{dom0SysGuiPolicyPackage}}/etc/qubes/policy.d/50-airlock-sys-gui.policy";
      sysGuiManager = lib.findFirst (
        package: packageName package == "qubes-manager"
      ) (throw "missing sys-gui Qubes Manager") sysGuiCfg.environment.systemPackages;
      has = name: values: builtins.elem name values;
      etcNames = cfg: lib.attrNames cfg.environment.etc;
      forbiddenConfigRpc = [
        "qubes-rpc/airlock.Config.ArchiveCandidate"
        "qubes-rpc/airlock.Config.ArchiveDeployment"
        "qubes-rpc/airlock.Config.CandidateExport"
        "qubes-rpc/airlock.Config.InputsImport"
        "qubes-rpc/airlock.Config.ValidateCandidate"
        "qubes-rpc/airlock.PolicyProbe"
      ];
      secretEnvNames = [
        "CODEX_AUTH_TOKEN"
        "OPENAI_API_KEY"
        "OPENAI_API_KEY_FILE"
      ];
      devServiceScript = devCfg.systemd.services.airlock-cmd-guest-init.script;
    in
    {{
      role = devCfg.airlock.qubeGuest.role;
      hostname = devCfg.networking.hostName;
      staged_dev_declared = builtins.hasAttr "usr-cmd" stagedNixNativeDomains;
      staged_sys_net_declared = builtins.hasAttr "sys-net" stagedNixNativeDomains;
      staged_sys_net_autostart = sysNetDomainDefinition.prefs.autostart;
      staged_sys_net_virt_mode = sysNetDomainDefinition.prefs.virt_mode;
      staged_sys_net_updates_proxy = sysNetDomainDefinition.services."qubes-updates-proxy";
      staged_sys_firewall_declared = builtins.hasAttr "sys-firewall" stagedNixNativeDomains;
      staged_sys_firewall_netvm = sysFirewallDomainDefinition.netvm;
      staged_sys_firewall_autostart = sysFirewallDomainDefinition.prefs.autostart;
      staged_sys_firewall_provides_network =
        sysFirewallDomainDefinition.prefs.provides_network;
      staged_sys_firewall_servicevm = sysFirewallDomainDefinition.features.servicevm;
      staged_sys_firewall_updates_proxy =
        sysFirewallDomainDefinition.services."qubes-updates-proxy";
      staged_sys_gui_declared = builtins.hasAttr "sys-gui" stagedNixNativeDomains;
      staged_sys_gui_role = sysGuiDomainDefinition.role;
      staged_sys_gui_netvm = sysGuiDomainDefinition.netvm;
      staged_sys_gui_guivm = sysGuiDomainDefinition.prefs.guivm;
      staged_sys_gui_autostart = sysGuiDomainDefinition.prefs.autostart;
      staged_sys_gui_agent_feature = sysGuiDomainDefinition.features.gui;
      staged_sys_gui_service = sysGuiDomainDefinition.services.guivm;
      staged_default_guivm = defaultRuntime.topology.extraPrefs.default_guivm;
      staged_default_netvm = defaultRuntime.topology.extraPrefs.default_netvm;
      generated_dev_domain_netvm = generatedDevDomain.netvm;
      generated_dev_domain_role_feature = generatedDevDomain.features."airlock-role";
      generated_dev_domain_marker_feature = generatedDevDomain.features."airlock-cmd";
      bootstrap_dev_domain_present = builtins.hasAttr "usr-cmd" bootstrapDomains;
      bootstrap_sys_net_present = builtins.hasAttr "sys-net" bootstrapDomains;
      bootstrap_sys_firewall_present = builtins.hasAttr "sys-firewall" bootstrapDomains;
      bootstrap_sys_net_autostart = bootstrapSysNetDomain.prefs.autostart;
      bootstrap_sys_net_netvm = bootstrapSysNetDomain.netvm;
      bootstrap_sys_net_virt_mode = bootstrapSysNetDomain.prefs.virt_mode;
      bootstrap_sys_net_updates_proxy = bootstrapSysNetDomain.services."qubes-updates-proxy";
      bootstrap_sys_firewall_netvm = bootstrapSysFirewallDomain.netvm;
      bootstrap_sys_firewall_autostart = bootstrapSysFirewallDomain.prefs.autostart;
      bootstrap_sys_firewall_provides_network =
        bootstrapSysFirewallDomain.prefs.provides_network;
      bootstrap_sys_firewall_servicevm = bootstrapSysFirewallDomain.features.servicevm;
      bootstrap_sys_firewall_updates_proxy =
        bootstrapSysFirewallDomain.services."qubes-updates-proxy";
      bootstrap_sys_gui_present = builtins.hasAttr "sys-gui" bootstrapDomains;
      bootstrap_sys_gui_guivm = bootstrapSysGuiDomain.prefs.guivm;
      bootstrap_sys_gui_autostart = bootstrapSysGuiDomain.prefs.autostart;
      bootstrap_sys_gui_agent_feature = bootstrapSysGuiDomain.features.gui;
      bootstrap_default_guivm = dom0Cfg.airlock.qubes.prefs.default_guivm;
      bootstrap_topology_default_guivm = dom0Cfg.airlock.qubes.topology.prefs.default_guivm;
      sys_gui_role_enabled = sysGuiCfg.airlock.sysGuiGuest.enable;
      sys_gui_agent_enabled = builtins.elem "multi-user.target"
        sysGuiCfg.systemd.services.qubes-gui-agent.wantedBy;
      sys_gui_agent_path = sysGuiCfg.systemd.services.qubes-gui-agent.environment.PATH;
      sys_gui_pam = sysGuiCfg.security.pam.services.qubes-gui-agent.text;
      sys_gui_session_launcher = builtins.readFile
        sysGuiCfg.environment.etc."qubes-rpc/qubes.GuiVMSession".source;
      sys_gui_login_xserver = builtins.readFile sysGuiCfg.services.xserver.displayManager.xserverBin;
      sys_gui_login_enabled = sysGuiCfg.services.xserver.displayManager.lightdm.enable;
      sys_gui_autologin = sysGuiCfg.services.displayManager.autoLogin.enable;
      sys_gui_login_wanted_by = sysGuiCfg.systemd.services.display-manager.wantedBy;
      sys_gui_login_password = sysGuiCfg.users.users.user.initialPassword;
      dom0_greeter_enabled = dom0Cfg.services.xserver.displayManager.lightdm.greeter.enable;
      dom0_autologin = dom0Cfg.services.displayManager.autoLogin.enable;
      sys_gui_template_tools_removed =
        sysGuiManager.passthru.airlockTemplateToolsRemoved or false;
      dom0_xfce_enabled = dom0Cfg.services.xserver.desktopManager.xfce.enable;
      dom0_xterm_enabled = dom0Cfg.services.xserver.desktopManager.xterm.enable;
      sys_gui_rpc_present = lib.all (name: builtins.hasAttr name sysGuiCfg.environment.etc) [
        "qubes-rpc/qubes.GuiVMSession"
        "qubes-rpc/qubes.SetMonitorLayout"
        "qubes-rpc/qubes.WindowIconUpdater"
      ];
      dom0_sys_gui_policy_present = has "airlock-sys-gui-policy" dom0QubesPackages;
      dom0_sys_gui_session_policy = lib.hasInfix
        "qubes.GuiVMSession * dom0 sys-gui allow"
        dom0SysGuiPolicy
        && lib.hasInfix "qubes.GuiVMSession * @anyvm @anyvm deny" dom0SysGuiPolicy;
      dom0_sys_gui_system_domain_visibility = lib.all (name:
        lib.hasInfix "admin.vm.List * sys-gui ${{name}} allow target=dom0" dom0SysGuiPolicy
        && lib.hasInfix "admin.Events * sys-gui ${{name}} allow target=dom0" dom0SysGuiPolicy
      ) (lib.filter (name: lib.hasPrefix "sys-" name)
        (builtins.attrNames dom0Cfg.airlock.qubes.topology.domains));
      dom0_sys_gui_management_policy = lib.all (rule: lib.hasInfix rule dom0SysGuiPolicy) [
        "admin.vm.Create.AppVM * sys-gui dom0 allow"
        "admin.vm.Create.StandaloneVM * sys-gui dom0 allow"
        "admin.vm.property.Get * sys-gui @anyvm allow target=dom0"
        "admin.vm.Start * sys-gui @anyvm allow target=dom0"
        "admin.vm.Remove * sys-gui @anyvm allow target=dom0"
        "* * sys-gui dom0 deny"
        "* * sys-gui @anyvm deny"
      ] && !(lib.hasInfix "admin.*" dom0SysGuiPolicy);
      dom0_sys_gui_session_present = lib.any (
        package: builtins.elem "sys-gui" (package.providedSessions or [ ])
      ) dom0Cfg.services.displayManager.sessionPackages;
      dom0_default_session = dom0Cfg.services.displayManager.defaultSession;
      dom0_display_manager_after = dom0Cfg.systemd.services.display-manager.after;
      dom0_bootstrap_status_device = dom0Cfg.airlock.qubes.setupConfigOnBoot.statusDevice;
      dom0_bootstrap_status_environment =
        dom0Cfg.systemd.services.airlock-setup-config.environment.AIRLOCK_SETUP_CONFIG_STATUS_DEVICE;
      bootstrap_default_netvm = dom0Cfg.airlock.qubes.prefs.default_netvm;
      bootstrap_topology_default_netvm = dom0Cfg.airlock.qubes.topology.prefs.default_netvm;
      bootstrap_dev_topology_present = builtins.hasAttr "usr-cmd" dom0Cfg.airlock.qubes.topology.domains;
      bootstrap_sys_net_topology_autostart = bootstrapTopologySysNetDomain.prefs.autostart;
      bootstrap_sys_firewall_topology_netvm = bootstrapTopologySysFirewallDomain.netvm;
      bootstrap_sys_firewall_topology_autostart =
        bootstrapTopologySysFirewallDomain.prefs.autostart;
      image_dev_mode_enabled = dom0Cfg.airlock.image.devMode.enable;
      dom0_dhcp_enabled = dom0Cfg.networking.useDHCP;
      dom0_dhcpcd_no_gateway = lib.hasInfix "nogateway" dom0Cfg.networking.dhcpcd.extraConfig;
      dom0_proxy_enabled = dom0Cfg.airlock.dom0.devModeNetworkProxy.enable;
      dom0_proxy_target = dom0Cfg.airlock.dom0.devModeNetworkProxy.target;
      dom0_proxy_http = dom0Cfg.environment.sessionVariables.http_proxy or null;
      dom0_proxy_https = dom0Cfg.environment.sessionVariables.https_proxy or null;
      dom0_proxy_no_proxy = dom0Cfg.environment.sessionVariables.no_proxy or null;
      dom0_nix_daemon_proxy_http =
        dom0Cfg.systemd.services.nix-daemon.environment.http_proxy or null;
      dom0_dev_mode_marker = dom0Cfg.environment.etc."airlock/dev-mode".text;
      dom0_observability_enabled = dom0Cfg.airlock.dom0.observability.enable;
      dom0_testing_tools_enabled = dom0Cfg.airlock.dom0.testingTools.enable;
      release_image_dev_mode_enabled = releaseDom0Cfg.airlock.image.devMode.enable;
      release_dom0_dhcp_enabled = releaseDom0Cfg.networking.useDHCP;
      release_dom0_dhcpcd_no_gateway =
        lib.hasInfix "nogateway" releaseDom0Cfg.networking.dhcpcd.extraConfig;
      release_dom0_proxy_enabled = releaseDom0Cfg.airlock.dom0.devModeNetworkProxy.enable;
      release_dom0_proxy_http = releaseDom0Cfg.environment.sessionVariables.http_proxy or null;
      release_dom0_dev_marker_absent =
        !(builtins.hasAttr "airlock/dev-mode" releaseDom0Cfg.environment.etc);
      release_dom0_observability_enabled = releaseDom0Cfg.airlock.dom0.observability.enable;
      release_dom0_testing_tools_enabled = releaseDom0Cfg.airlock.dom0.testingTools.enable;
      release_bootstrap_dev_domain_present = builtins.hasAttr "usr-cmd" releaseBootstrapDomains;
      release_bootstrap_sys_net_present = builtins.hasAttr "sys-net" releaseBootstrapDomains;
      release_bootstrap_sys_net_netvm = releaseBootstrapDomains.sys-net.netvm;
      release_bootstrap_sys_net_autostart = releaseBootstrapDomains.sys-net.prefs.autostart;
      release_bootstrap_sys_net_virt_mode =
        releaseBootstrapDomains.sys-net.prefs.virt_mode;
      release_bootstrap_sys_firewall_present =
        builtins.hasAttr "sys-firewall" releaseBootstrapDomains;
      release_bootstrap_sys_firewall_netvm = releaseBootstrapDomains.sys-firewall.netvm;
      release_bootstrap_sys_firewall_autostart =
        releaseBootstrapDomains.sys-firewall.prefs.autostart;
      non_dev_image_dev_mode_enabled = nonDevDom0Cfg.airlock.image.devMode.enable;
      non_dev_dom0_dhcp_enabled = nonDevDom0Cfg.networking.useDHCP;
      non_dev_dom0_dhcpcd_no_gateway =
        lib.hasInfix "nogateway" nonDevDom0Cfg.networking.dhcpcd.extraConfig;
      non_dev_dom0_proxy_enabled = nonDevDom0Cfg.airlock.dom0.devModeNetworkProxy.enable;
      non_dev_dom0_proxy_http = nonDevDom0Cfg.environment.sessionVariables.http_proxy or null;
      non_dev_dom0_dev_marker_absent =
        !(builtins.hasAttr "airlock/dev-mode" nonDevDom0Cfg.environment.etc);
      non_dev_dom0_observability_enabled = nonDevDom0Cfg.airlock.dom0.observability.enable;
      non_dev_dom0_testing_tools_enabled = nonDevDom0Cfg.airlock.dom0.testingTools.enable;
      network_client_resolvconf_disabled = !devCfg.networking.resolvconf.enable;
      private_nix_enabled = devCfg.airlock.minimalGuest.nix.enable;
      nix_features = devCfg.nix.settings.experimental-features;
      nix_allowed_users = devCfg.nix.settings.allowed-users;
      codex_home = devCfg.airlock.qubeGuest.codexHome;
      codex_home_env = devCfg.environment.sessionVariables.CODEX_HOME or null;
      dev_persistent_paths = devCfg.airlock.qubeGuest.cmdPersistentPaths;
      dev_init_contains_dev_path = lib.hasInfix "/home/user/dev" devServiceScript;
      dev_init_contains_codex_home = lib.hasInfix "/home/user/.codex" devServiceScript;
      dev_init_contains_private_nix_root = lib.hasInfix "/rw/home/user/airlock-config-nix-root" devServiceScript;
      dev_qrexec_requires = devCfg.systemd.services.qubes-qrexec-agent.requires;
      dev_qrexec_after = devCfg.systemd.services.qubes-qrexec-agent.after;
      dev_forbidden_rpc_absent = lib.all (name: !(has name (etcNames devCfg))) forbiddenConfigRpc;
      retired_config_policy_enabled = dom0Cfg.airlock.qubes.configAccessPolicy.enable;
      dev_no_secret_env = lib.all (name: !(builtins.hasAttr name devCfg.environment.sessionVariables)) secretEnvNames;
      dev_no_codex_auth_etc = lib.all (
        name:
        !(lib.hasInfix "auth.json" name)
        && !(lib.hasInfix "OPENAI_API_KEY" name)
        && !(lib.hasInfix ".codex" name)
      ) (etcNames devCfg);
      base_role = baseCfg.airlock.qubeGuest.role;
      base_private_nix_enabled = baseCfg.airlock.minimalGuest.nix.enable;
      base_forbidden_rpc_absent = lib.all (name: !(has name (etcNames baseCfg))) forbiddenConfigRpc;
    }}
    """
    _run(["nix", "eval", "--impure", "--json", "--expr", expr], stdout=role_json)
    data = json.loads(role_json.read_text(encoding="utf-8"))

    assert data["role"] == "cmd"
    assert data["hostname"] == "sample-cmd"
    assert data["staged_dev_declared"] is True
    assert data["staged_sys_net_declared"] is True
    assert data["staged_sys_net_autostart"] is True
    assert data["staged_sys_net_virt_mode"] == "hvm"
    assert data["staged_sys_net_updates_proxy"] is True
    assert data["staged_sys_firewall_declared"] is True
    assert data["staged_sys_firewall_netvm"] == "sys-net"
    assert data["staged_sys_firewall_autostart"] is True
    assert data["staged_sys_firewall_provides_network"] is True
    assert data["staged_sys_firewall_servicevm"] == "1"
    assert data["staged_sys_firewall_updates_proxy"] is True
    assert data["staged_sys_gui_declared"] is True
    assert data["staged_sys_gui_role"] == "sys-gui"
    assert data["staged_sys_gui_netvm"] is None
    assert data["staged_sys_gui_guivm"] == "dom0"
    assert data["staged_sys_gui_autostart"] is True
    assert data["staged_sys_gui_agent_feature"] == "1"
    assert data["staged_sys_gui_service"] is True
    assert data["staged_default_guivm"] == "sys-gui"
    assert data["staged_default_netvm"] == "sys-firewall"
    assert data["generated_dev_domain_netvm"] is None
    assert data["generated_dev_domain_role_feature"] == "cmd"
    assert data["generated_dev_domain_marker_feature"] == "1"
    assert data["bootstrap_dev_domain_present"] is True
    assert data["bootstrap_sys_net_present"] is True
    assert data["bootstrap_sys_firewall_present"] is True
    assert data["bootstrap_sys_net_autostart"] is True
    assert data["bootstrap_sys_net_netvm"] is None
    assert data["bootstrap_sys_net_virt_mode"] == "hvm"
    assert data["bootstrap_sys_net_updates_proxy"] is True
    assert data["bootstrap_sys_firewall_netvm"] == "sys-net"
    assert data["bootstrap_sys_firewall_autostart"] is True
    assert data["bootstrap_sys_firewall_provides_network"] is True
    assert data["bootstrap_sys_firewall_servicevm"] == "1"
    assert data["bootstrap_sys_firewall_updates_proxy"] is True
    assert data["bootstrap_sys_gui_present"] is True
    assert data["bootstrap_sys_gui_guivm"] == "dom0"
    assert data["bootstrap_sys_gui_autostart"] is True
    assert data["bootstrap_sys_gui_agent_feature"] == "1"
    assert data["bootstrap_default_guivm"] == "sys-gui"
    assert data["bootstrap_topology_default_guivm"] == "sys-gui"
    assert data["sys_gui_role_enabled"] is True
    assert data["sys_gui_agent_enabled"] is True
    agent_path = data["sys_gui_agent_path"].split(":")
    assert any(
        (Path(_physical_store_path(repo_root, directory)) / "runuser").is_file()
        for directory in agent_path
    ), "sys-gui must be able to start Xorg with runuser from its service PATH"
    assert "pam_systemd.so" in data["sys_gui_pam"]
    assert "system-auth" not in data["sys_gui_pam"]
    assert "postlogin" not in data["sys_gui_pam"]
    launcher = data["sys_gui_session_launcher"]
    assert "start display-manager.service" in launcher
    assert "QREXEC_REMOTE_DOMAIN" in launcher
    assert "qubes-gui-runuser" not in launcher
    assert "> ~/.xsession-errors" not in launcher
    assert "-sw-cursor" in data["sys_gui_login_xserver"]
    assert "-ac" not in data["sys_gui_login_xserver"]
    assert data["sys_gui_login_enabled"] is True
    assert data["sys_gui_autologin"] is False
    assert data["sys_gui_login_wanted_by"] == []
    assert data["sys_gui_login_password"] == "airlock"
    assert data["dom0_greeter_enabled"] is False
    assert data["dom0_autologin"] is True
    assert data["sys_gui_template_tools_removed"] is True
    assert data["dom0_xfce_enabled"] is False
    assert data["dom0_xterm_enabled"] is False
    assert data["sys_gui_rpc_present"] is True
    assert data["dom0_sys_gui_policy_present"] is True
    assert data["dom0_sys_gui_session_policy"] is True
    assert data["dom0_sys_gui_system_domain_visibility"] is True
    assert data["dom0_sys_gui_management_policy"] is True
    assert data["dom0_sys_gui_session_present"] is True
    assert data["dom0_default_session"] == "sys-gui"
    assert "airlock-setup-config.service" in data["dom0_display_manager_after"]
    assert data["dom0_bootstrap_status_device"] == "/dev/tty1"
    assert data["dom0_bootstrap_status_environment"] == "/dev/tty1"
    assert data["bootstrap_default_netvm"] == "sys-firewall"
    assert data["bootstrap_topology_default_netvm"] == "sys-firewall"
    assert data["bootstrap_dev_topology_present"] is True
    assert data["bootstrap_sys_net_topology_autostart"] is True
    assert data["bootstrap_sys_firewall_topology_netvm"] == "sys-net"
    assert data["bootstrap_sys_firewall_topology_autostart"] is True
    assert data["image_dev_mode_enabled"] is True
    assert data["dom0_dhcp_enabled"] is True
    assert data["dom0_dhcpcd_no_gateway"] is True
    assert data["dom0_proxy_enabled"] is True
    assert data["dom0_proxy_target"] == "sys-firewall"
    assert data["dom0_proxy_http"] == "http://127.0.0.1:8082"
    assert data["dom0_proxy_https"] == "http://127.0.0.1:8082"
    assert data["dom0_proxy_no_proxy"] == "127.0.0.1,localhost,::1"
    assert data["dom0_nix_daemon_proxy_http"] == "http://127.0.0.1:8082"
    assert data["dom0_dev_mode_marker"] == "enabled\n"
    assert data["dom0_observability_enabled"] is True
    assert data["dom0_testing_tools_enabled"] is True
    assert data["release_image_dev_mode_enabled"] is False
    assert data["release_dom0_dhcp_enabled"] is False
    assert data["release_dom0_dhcpcd_no_gateway"] is False
    assert data["release_dom0_proxy_enabled"] is False
    assert data["release_dom0_proxy_http"] is None
    assert data["release_dom0_dev_marker_absent"] is True
    assert data["release_dom0_observability_enabled"] is False
    assert data["release_dom0_testing_tools_enabled"] is False
    assert data["release_bootstrap_dev_domain_present"] is True
    assert data["release_bootstrap_sys_net_present"] is True
    assert data["release_bootstrap_sys_net_netvm"] is None
    assert data["release_bootstrap_sys_net_autostart"] is True
    assert data["release_bootstrap_sys_net_virt_mode"] == "hvm"
    assert data["release_bootstrap_sys_firewall_present"] is True
    assert data["release_bootstrap_sys_firewall_netvm"] == "sys-net"
    assert data["release_bootstrap_sys_firewall_autostart"] is True
    assert data["non_dev_image_dev_mode_enabled"] is False
    assert data["non_dev_dom0_dhcp_enabled"] is False
    assert data["non_dev_dom0_dhcpcd_no_gateway"] is False
    assert data["non_dev_dom0_proxy_enabled"] is False
    assert data["non_dev_dom0_proxy_http"] is None
    assert data["non_dev_dom0_dev_marker_absent"] is True
    assert data["non_dev_dom0_observability_enabled"] is False
    assert data["non_dev_dom0_testing_tools_enabled"] is False
    assert data["network_client_resolvconf_disabled"] is True
    assert data["private_nix_enabled"] is True
    assert "nix-command" in data["nix_features"]
    assert "flakes" in data["nix_features"]
    assert data["nix_allowed_users"] == ["user"]
    assert data["codex_home"] == "/home/user/.codex"
    assert data["codex_home_env"] == "/home/user/.codex"
    assert "/home/user/dev" in data["dev_persistent_paths"]
    assert data["dev_init_contains_dev_path"] is True
    assert data["dev_init_contains_codex_home"] is True
    assert data["dev_init_contains_private_nix_root"] is True
    assert "airlock-cmd-guest-init.service" in data["dev_qrexec_requires"]
    assert "airlock-cmd-guest-init.service" in data["dev_qrexec_after"]
    assert data["dev_forbidden_rpc_absent"] is True
    assert data["retired_config_policy_enabled"] is False
    assert data["dev_no_secret_env"] is True
    assert data["dev_no_codex_auth_etc"] is True
    assert data["base_role"] == "base"
    assert data["base_private_nix_enabled"] is False
    assert data["base_forbidden_rpc_absent"] is True

    codex_out = log_dir / "codex-out.txt"
    _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.codex",
            "--no-link",
            "--print-build-logs",
            "--print-out-paths",
        ],
        stdout=codex_out,
    )
    codex_path = codex_out.read_text(encoding="utf-8").splitlines()[-1]
    codex_version = _run(
        [
            "bash",
            "-c",
            'source "$1"; airlock_run_store_path "$2/bin/codex" --version',
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/lib/airlock-env.sh"),
            codex_path,
        ],
        cwd=repo_root,
    ).stdout
    (log_dir / "codex-version.txt").write_text(codex_version, encoding="utf-8")
    assert re.search(r"^codex-cli [0-9]+\.[0-9]+\.[0-9]+$", codex_version)
    codex_physical = Path(_physical_store_path(repo_root, codex_path))
    assert not any(codex_physical.rglob("auth.json"))

    zellij_out = log_dir / "zellij-out.txt"
    _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.zellij",
            "--no-link",
            "--print-build-logs",
            "--print-out-paths",
        ],
        stdout=zellij_out,
    )
    zellij_path = zellij_out.read_text(encoding="utf-8").splitlines()[-1]
    zellij_version = _run(
        [
            "bash",
            "-c",
            'source "$1"; airlock_run_store_path "$2/bin/zellij" --version',
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/lib/airlock-env.sh"),
            zellij_path,
        ],
        cwd=repo_root,
    ).stdout
    (log_dir / "zellij-version.txt").write_text(zellij_version, encoding="utf-8")
    assert re.search(r"^zellij [0-9]+\.[0-9]+\.[0-9]+$", zellij_version)


def check_airlock_minimal_guest(repo_root: Path, log_dir: Path) -> None:
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    result_json = log_dir / "result.json"
    expr = f"""
    let
      flake = builtins.getFlake "path:{repo_root}";
      lib = flake.inputs.nixpkgs.lib;
      cfgs = {{
        staging = flake.nixosConfigurations."airlock-domain-sys-ctrl".config;
        validator = flake.nixosConfigurations."airlock-domain-usr-assistant".config;
        vault = flake.nixosConfigurations."airlock-domain-sys-ctrl".config;
      }};
      cfgList = builtins.attrValues cfgs;
      enabledServices =
        cfg:
        lib.attrNames (
          lib.filterAttrs (_name: service: service.enable or false) cfg.systemd.services
        );
      etcNames = cfg: lib.attrNames cfg.environment.etc;
      has = name: values: builtins.elem name values;
      allCfgs = pred: lib.all pred cfgList;
      noConfigInputEtc =
        cfg:
        lib.all (name: !(lib.hasPrefix "airlock/config-inputs/" name)) (etcNames cfg);
    in
    {{
      enabled_services = lib.mapAttrs (_name: cfg: enabledServices cfg) cfgs;
      service_counts = lib.mapAttrs (_name: cfg: builtins.length (enabledServices cfg)) cfgs;
      minimal_enabled = allCfgs (cfg: cfg.airlock.minimalGuest.enable);
      documentation_disabled = allCfgs (
        cfg:
        cfg.documentation.enable == false
        && cfg.documentation.doc.enable == false
        && cfg.documentation.info.enable == false
        && cfg.documentation.man.enable == false
        && cfg.documentation.nixos.enable == false
      );
      perlless_mechanics_enabled = allCfgs (
        cfg:
        cfg.system.etc.overlay.enable == true
        && cfg.services.userborn.enable == true
      );
      installer_tools_disabled = allCfgs (cfg: cfg.system.disableInstallerTools);
      nix_cli_capability = {{
        staging = cfgs.staging.airlock.minimalGuest.nix.enable;
        validator = cfgs.validator.airlock.minimalGuest.nix.enable;
        vault = cfgs.vault.airlock.minimalGuest.nix.enable;
      }};
      stock_runtime_retained = allCfgs (
        cfg:
        (cfg.systemd.package.pname or "") == "systemd"
        && cfg.services.dbus.enable == true
        && cfg.networking.useDHCP == false
        && cfg.systemd.services ? "qubes-network-uplink@"
      );
      no_guest_config_input_etc = allCfgs noConfigInputEtc;
      required_qubes_services_retained = allCfgs (
        cfg:
        lib.all (name: has name (enabledServices cfg)) [
          "airlock-qubes-rw-init"
          "qubes-db"
          "qubes-qrexec-agent"
          "qubes-sysinit"
        ]
      );
      config_inputs_import_rpc = {{
        staging = cfgs.staging.environment.etc ? "qubes-rpc/airlock.Config.InputsImport";
        validator = cfgs.validator.environment.etc ? "qubes-rpc/airlock.Config.InputsImport";
        vault = cfgs.vault.environment.etc ? "qubes-rpc/airlock.Config.InputsImport";
      }};
    }}
    """
    _run(["nix", "eval", "--impure", "--json", "--expr", expr], stdout=result_json)
    data = json.loads(result_json.read_text(encoding="utf-8"))
    assert data["minimal_enabled"] is True
    assert data["documentation_disabled"] is True
    assert data["perlless_mechanics_enabled"] is True
    assert data["installer_tools_disabled"] is True
    assert data["nix_cli_capability"] == {
        "staging": True,
        "validator": True,
        "vault": True,
    }
    assert data["stock_runtime_retained"] is True
    assert data["no_guest_config_input_etc"] is True
    assert data["required_qubes_services_retained"] is True
    assert data["config_inputs_import_rpc"] == {
        "staging": False,
        "validator": False,
        "vault": False,
    }


def check_airlock_minimal_closure(repo_root: Path, log_dir: Path) -> None:
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    roles = ("sys-ctrl", "usr-assistant")
    paths = {}
    for role in roles:
        attr = f"{repo_root}#nixosConfigurations.airlock-domain-{role}.config.system.build.toplevel"
        paths[role] = _run(
            [
                "nix",
                "build",
                "--no-link",
                "--print-out-paths",
                attr,
                "--print-build-logs",
                "--show-trace",
            ]
        ).stdout.strip()
        closure_file = log_dir / f"closure-{role}.txt"
        _run(["nix", "path-info", "-r", paths[role]], stdout=closure_file)

    def closure(role: str) -> str:
        return (log_dir / f"closure-{role}.txt").read_text(encoding="utf-8")

    def assert_has(role: str, pattern: str, description: str) -> None:
        assert re.search(
            pattern, closure(role)
        ), f"missing expected {description} in {role} closure: {pattern}"

    def assert_absent(role: str, pattern: str, description: str) -> None:
        assert not re.search(
            pattern, closure(role)
        ), f"unexpected {description} in {role} closure: {pattern}"

    for role in roles:
        assert_has(
            role,
            r"/nix/store/[[:alnum:]]+-qubes-core-qrexec-domU-".replace(
                "[[:alnum:]]", "[A-Za-z0-9]"
            ),
            "qrexec domU runtime",
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-qubes-core-qubesdb-client-",
            "QubesDB client",
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-qubes-core-qubesdb-daemon-",
            "QubesDB daemon",
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-qubes-core-agent-linux-[0-9]",
            "upstream Qubes core agent package",
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-python[^/]*-qubes-core-qrexec",
            "upstream Python qrexec package",
        )
        assert_has(
            role, r"/nix/store/[A-Za-z0-9]+-systemd-[0-9]", "full systemd package"
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-userborn-",
            "userborn passwd/group generator",
        )
        assert_absent(
            role,
            r"/nix/store/[A-Za-z0-9]+-nixos-(build-vms|enter|generate-config|install|option)",
            "NixOS installer/admin tooling",
        )

    for role in roles:
        assert_has(role, r"/nix/store/[A-Za-z0-9]+-nix-[0-9]", "role-specific Nix CLI")
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-nix-flake",
            "role-specific nix flake support",
        )
        assert_has(
            role,
            r"/nix/store/[A-Za-z0-9]+-airlock-config-nix",
            "role-specific Airlock Nix helper",
        )

    role_reports = []
    for role in roles:
        closure_lines = closure(role).splitlines()
        path_info = json.loads(
            _run(
                ["nix", "path-info", "--json-format", "1", "--json", "-S", paths[role]]
            ).stdout
        )
        role_reports.append(
            {
                "role": role,
                "path": paths[role],
                "store_paths": len(closure_lines),
                "closure_size_bytes": next(iter(path_info.values()))["closureSize"],
            }
        )
    (log_dir / "result.json").write_text(
        json.dumps(
            {
                "schema": "airlock.minimal-closure-test.v1",
                "status": "passed",
                "roles": role_reports,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
