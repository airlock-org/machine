{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.airlock.machineDeployment;
  engine = pkgs.callPackage ../../runtime/tools/pkgs/airlock-machine-deploy { };
  setup = pkgs.callPackage ../tools/pkgs/airlock-setup-config { };
  supervisor = pkgs.callPackage ../../runtime/tools/pkgs/airlock-boot-supervisor { };
  retiredPolicy = pkgs.writeTextDir "etc/qubes/policy.d/05-airlock-retired.policy" ''
    # A retired domain remains registered while its data is recoverable.  Its
    # tag is applied before the deployment changes the active topology.
    * * @tag:airlock-retired @anyvm deny
    * * @anyvm @tag:airlock-retired deny
  '';
  guardedGui = config.airlock.guiGpu.enable && config.airlock.guiGpu.startOnBoot;
  guestControl = action: pkgs.writeShellScript "airlock-ctrl-guest-${action}" ''
    set -eu
    [ "$#" -eq 0 ] || exit 2
    exec ${engine}/bin/airlock-machine-deploy guest-${action} -
  '';
  guestPlan = guestControl "plan";
  guestApply = guestControl "apply";
  machineControl = action: pkgs.writeShellScript "airlock-ctrl-machine-${action}" ''
    set -eu
    [ "$#" -eq 0 ] || exit 2
    export PATH=${lib.makeBinPath [ pkgs.nix pkgs.systemd ]}:"$PATH"
    exec ${pkgs.python3}/bin/python3 ${../../runtime/tools/sys-ctrl-machine-receive.py} ${action}
  '';
  machinePlan = machineControl "plan";
  machineApply = machineControl "apply";
  guestHandler = action: helper: pkgs.writeShellScript "airlock-ctrl-guest-${action}-qrexec" ''
    set -eu
    [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-ctrl ] || exit 1
    exec /run/wrappers/bin/sudo -n ${helper}
  '';
  guestPolicy = pkgs.writeTextDir "etc/qubes/policy.d/43-airlock-ctrl-guest.policy" ''
    airlock.Ctrl.GuestDeploy * sys-dev sys-ctrl allow
    airlock.Ctrl.GuestDeploy * @anyvm @anyvm deny
    airlock.Ctrl.GuestPlan * sys-ctrl dom0 allow
    airlock.Ctrl.GuestPlan * @anyvm @anyvm deny
    airlock.Ctrl.GuestApply * sys-ctrl dom0 allow
    airlock.Ctrl.GuestApply * @anyvm @anyvm deny
    airlock.Ctrl.MachinePlan * sys-ctrl dom0 allow
    airlock.Ctrl.MachinePlan * @anyvm @anyvm deny
    airlock.Ctrl.MachineApply * sys-ctrl dom0 allow
    airlock.Ctrl.MachineApply * @anyvm @anyvm deny
  '';
  bootPrerequisites = [
    "airlock-setup-config.service"
  ]
  ++ lib.optional guardedGui "airlock-start-gui-gpu.service"
  ++ lib.optional config.airlock.qubes.startNetworkProvidersOnBoot.enable "airlock-start-network-providers.service"
  ++ lib.optional config.airlock.qubes.deferredSetupConfigOnBoot.enable "airlock-setup-config-deferred.service";
in
{
  options.airlock.machineDeployment = {
    enable = lib.mkEnableOption "persistent repository-owned machine deployments";
    profile = lib.mkOption {
      type = lib.types.str;
      default = "default";
    };
    retireDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Domains intentionally removed by this release and moved to recoverable trash.";
    };
    trashRetentionDays = lib.mkOption {
      type = lib.types.ints.between 1 3650;
      default = 30;
      description = "Minimum days before an unreferenced trashed domain is eligible for explicit purge.";
    };
  };
  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ engine ];
    virtualisation.qubes.packages = [ retiredPolicy guestPolicy ];
    environment.etc."qubes-rpc/airlock.Ctrl.GuestPlan" = {
      source = guestHandler "plan" guestPlan;
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.Ctrl.GuestApply" = {
      source = guestHandler "apply" guestApply;
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.Ctrl.MachinePlan" = {
      source = guestHandler "machine-plan" machinePlan;
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.Ctrl.MachineApply" = {
      source = guestHandler "machine-apply" machineApply;
      mode = "0555";
    };
    security.sudo.extraRules = [{
      users = [ config.airlock.dom0.user ];
      commands = [
        { command = "${guestPlan}"; options = [ "NOPASSWD" ]; }
        { command = "${guestApply}"; options = [ "NOPASSWD" ]; }
        { command = "${machinePlan}"; options = [ "NOPASSWD" ]; }
        { command = "${machineApply}"; options = [ "NOPASSWD" ]; }
      ];
    }];
    environment.etc."airlock/machine-profile".text = cfg.profile + "\n";
    systemd.services = lib.mkMerge [
      (lib.mkIf config.airlock.qubes.setupConfigOnBoot.enable {
        airlock-machine-autostart = {
          description = "Start declared autostart VMs after guarded bootstrap";
          wantedBy = [ "multi-user.target" ];
          requires = bootPrerequisites;
          after = bootPrerequisites;
          restartIfChanged = false;
          stopIfChanged = false;
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = "${engine}/bin/airlock-machine-deploy boot-start${lib.optionalString guardedGui " --require-running sys-gui"}";
            ExecStartPost = [
              "${supervisor}/bin/airlock-boot-supervisor complete 'all declared autostart guests are qrexec-ready'"
              "${pkgs.systemd}/bin/journalctl --sync"
            ];
          };
        };
        airlock-setup-config.serviceConfig.ExecStart = lib.mkForce "${engine}/bin/airlock-machine-deploy boot-setup ${setup}/bin/airlock-setup-config --apply${
          lib.optionalString (
            config.airlock.qubes.setupConfigOnBoot.scope == "boot-critical"
          ) " --boot-critical"
        }";
      })
      (lib.mkIf config.airlock.qubes.deferredSetupConfigOnBoot.enable {
        airlock-setup-config-deferred.serviceConfig.ExecStart = lib.mkForce "${engine}/bin/airlock-machine-deploy boot-setup ${setup}/bin/airlock-setup-config --apply --deferred";
      })
    ];
  };
}
