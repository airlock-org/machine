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
    virtualisation.qubes.packages = [ retiredPolicy ];
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
