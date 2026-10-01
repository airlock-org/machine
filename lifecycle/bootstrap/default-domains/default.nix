{
  lib,
  mkAirlockQubeBuilders,
  airlockConfigInputs,
  airlockQubesVmKernelName,
  networkDebug ? false,
}:
let
  sysNetPrivateSizeBytes = 2147483648;

  sysNetRoleDefaults = import ./sys-net.nix { inherit sysNetPrivateSizeBytes; };
  sysAgentRoleDefaults = import ./sys-agent.nix;
  sysCtrlRoleDefaults = import ./sys-ctrl.nix;
  sysFirewallRoleDefaults = import ./sys-firewall.nix { inherit networkDebug; };
  sysGuiRoleDefaults = import ./sys-gui.nix;
  cmdVmRoleDefaults = import ./usr-cmd.nix;
  homeVmRoleDefaults = import ./usr-home.nix;

  mkSysNetDomainEntry =
    system:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "sys-net";
      guestRole = "sys-net";
      modules = [
        (
          { ... }:
          {
            networking.hostName = sysNetRoleDefaults.guestHostName;
          }
          // lib.optionalAttrs networkDebug {
            airlock.qubeGuest.allowQrexecUserSelection = true;
            systemd.services.NetworkManager.wantedBy = lib.mkForce [ ];
          }
        )
      ];
      rootImageSizeMiB = sysNetRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = sysNetRoleDefaults.label;
      netvm = sysNetRoleDefaults.netvm;
      prefs = sysNetRoleDefaults.prefs;
      features = sysNetRoleDefaults.features;
      services = sysNetRoleDefaults.services;
      devices = sysNetRoleDefaults.devices;
      state = sysNetRoleDefaults.state;
    };

  mkSysAgentDomainEntryWith =
    system:
    { extraSpecialArgs ? { } }:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "sys-agent";
      guestRole = "sys-agent";
      inherit extraSpecialArgs;
      modules = [
        ({ ... }: { networking.hostName = sysAgentRoleDefaults.guestHostName; })
        ../../misc/modules/sys-agent-guest.nix
      ];
      rootImageSizeMiB = sysAgentRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = sysAgentRoleDefaults.label;
      netvm = sysAgentRoleDefaults.netvm;
      prefs = sysAgentRoleDefaults.prefs;
      features = sysAgentRoleDefaults.features;
      services = sysAgentRoleDefaults.services;
      devices = sysAgentRoleDefaults.devices;
      state = sysAgentRoleDefaults.state;
    };

  mkSysAgentDomainEntry = system: mkSysAgentDomainEntryWith system { };

  mkSysCtrlDomainEntryWith =
    system: { extraSpecialArgs ? { } }:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "sys-ctrl";
      guestRole = "sys-ctrl";
      inherit extraSpecialArgs;
      modules = [
        ({ ... }: {
          networking.hostName = sysCtrlRoleDefaults.guestHostName;
          airlock.qubeGuest.configRepoPath = "/home/user/dev/airlock-machine";
          # Bound concurrent builds in the controller's fixed memory budget.
          nix.settings.max-jobs = 1;
        })
        ../../misc/modules/machine-source-seed.nix
        ../../misc/modules/sys-agent-build-store.nix
        ../../misc/modules/sys-agent-machine-build.nix
      ];
      rootImageSizeMiB = sysCtrlRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = sysCtrlRoleDefaults.label;
      netvm = sysCtrlRoleDefaults.netvm;
      prefs = sysCtrlRoleDefaults.prefs;
      features = sysCtrlRoleDefaults.features;
      services = sysCtrlRoleDefaults.services;
      state = sysCtrlRoleDefaults.state;
    };

  mkSysCtrlDomainEntry = system: mkSysCtrlDomainEntryWith system { };

  mkSysFirewallDomainEntry =
    system:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "sys-firewall";
      guestRole = "sys-firewall";
      modules = [
        ({ ... }: {
          networking.hostName = sysFirewallRoleDefaults.guestHostName;
        })
      ];
      rootImageSizeMiB = sysFirewallRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = sysFirewallRoleDefaults.label;
      netvm = sysFirewallRoleDefaults.netvm;
      prefs = sysFirewallRoleDefaults.prefs;
      features = sysFirewallRoleDefaults.features;
      services = sysFirewallRoleDefaults.services;
    };

  mkSysGuiDomainEntryWith =
    system:
    {
      defaultApplicationVM ? "usr-home",
      configurationName ? "airlock-domain-sys-gui",
      extraModules ? [ ],
    }:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "sys-gui";
      guestRole = "sys-gui";
      inherit configurationName;
      modules = [
        ({ ... }: {
          networking.hostName = sysGuiRoleDefaults.guestHostName;
          airlock.sysGuiGuest.defaultApplicationVM = defaultApplicationVM;
          # Development/bootstrap login, matching the nested image password.
          # Real deployments supply their own NixOS password configuration.
          users.users.user.initialPassword = "airlock";
        })
      ] ++ extraModules;
      rootImageSizeMiB = sysGuiRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = sysGuiRoleDefaults.label;
      netvm = sysGuiRoleDefaults.netvm;
      prefs = sysGuiRoleDefaults.prefs;
      features = sysGuiRoleDefaults.features;
      services = sysGuiRoleDefaults.services;
    };

  mkSysGuiDomainEntry = system: mkSysGuiDomainEntryWith system { };

  mkCmdVmDomainEntry =
    system:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      name = "usr-cmd";
      guestRole = "cmd";
      modules = [
        ({ ... }: {
          networking.hostName = cmdVmRoleDefaults.guestHostName;
        })
      ];
      rootImageSizeMiB = cmdVmRoleDefaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = cmdVmRoleDefaults.label;
      netvm = cmdVmRoleDefaults.netvm;
      prefs = cmdVmRoleDefaults.prefs;
      features = cmdVmRoleDefaults.features;
      services = cmdVmRoleDefaults.services;
      state = cmdVmRoleDefaults.state;
    };

  mkPersonalVmDomainEntry =
    system: name: defaults: guestModule:
    let
      qubeBuilders = mkAirlockQubeBuilders system;
    in
    qubeBuilders.mkNixNativeDomain {
      inherit name;
      guestRole = "base";
      modules = [
        ({ lib, ... }: {
          networking.hostName = defaults.guestHostName;
          # Current lab GNOME display uses 3x Xwayland rendering at 250%.
          # Override this for other monitors; automatic scale sync is future work.
          airlock.appGui.scale = lib.mkDefault 3;
        })
        guestModule
      ];
      rootImageSizeMiB = defaults.rootImageSizeMiB;
      kernelName = airlockQubesVmKernelName;
      label = defaults.label;
      netvm = defaults.netvm;
      prefs = defaults.prefs;
      features = defaults.features;
      services = defaults.services;
      state = defaults.state;
    };

  mkHomeVmDomainEntry = system:
    mkPersonalVmDomainEntry system "usr-home" homeVmRoleDefaults ../../misc/modules/usr-home-guest.nix;

  materializeNixClosureDomain =
    entry:
    entry.domain
    // {
      prefs = entry.domain.prefs // {
        kernelopts = entry.kernelopts;
      };
      backend = entry.domain.backend // {
        system = entry.domain.backend.system // {
          source = "${entry.configuration.config.system.build.toplevel}";
          sourceRef = null;
        };
        root = entry.domain.backend.root // {
          source = "${entry.rootImage}/root.img";
          sourceRef = null;
        };
        store = entry.domain.backend.store // {
          source = "${entry.storeArtifact}";
          sourceRef = null;
        };
      };
    };
in
{
  inherit
    materializeNixClosureDomain
    mkCmdVmDomainEntry
    mkHomeVmDomainEntry
    mkSysFirewallDomainEntry
    mkSysGuiDomainEntry
    mkSysGuiDomainEntryWith
    mkSysNetDomainEntry
    mkSysAgentDomainEntry
    mkSysAgentDomainEntryWith
    mkSysCtrlDomainEntry
    mkSysCtrlDomainEntryWith
    sysFirewallRoleDefaults
    sysGuiRoleDefaults
    sysNetRoleDefaults
    ;
}
