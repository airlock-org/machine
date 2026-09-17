{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.airlock.machineDeployment;
  engine = pkgs.callPackage ../../runtime/tools/pkgs/airlock-machine-deploy { };
  poolArtifacts = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
  setup = pkgs.callPackage ../tools/pkgs/airlock-setup-config { };
  poolAdmission = cfg.poolAdmission;
  poolAdmissionTargets =
    if poolAdmission.targets == [ ] then
      "__airlock_no_pool_admission_target__"
    else
      lib.concatStringsSep "|" poolAdmission.targets;
  poolAdmissionHandler = pkgs.writeShellScript "airlock-pool-admit-qrexec" ''
    set -eu

    remote="''${QREXEC_REMOTE_DOMAIN:-}"
    target="''${QREXEC_SERVICE_ARGUMENT:-}"
    [ "$remote" = ${lib.escapeShellArg poolAdmission.caller} ] || {
      printf 'airlock.Pool.Admit is restricted to %s\n' ${lib.escapeShellArg poolAdmission.caller} >&2
      exit 1
    }
    case "$target" in
      ${poolAdmissionTargets}) ;;
      *)
        printf 'airlock.Pool.Admit target is not permitted: %s\n' "$target" >&2
        exit 1
        ;;
    esac
    exec ${poolArtifacts}/bin/airlock-pool-artifact receive \
      --expected-domain "$target" \
      --approved-by "$remote"
  '';
  poolAdmissionPolicy = pkgs.writeTextDir "etc/qubes/policy.d/42-airlock-pool-admission.policy" ''
    # Managed by Airlock. This service admits an inactive guest store generation.
    ${lib.concatMapStringsSep "\n" (target: "airlock.Pool.Admit +${target} ${poolAdmission.caller} dom0 allow") poolAdmission.targets}
    airlock.Pool.Admit * @anyvm @anyvm deny
  '';
  guardedGui = config.airlock.guiGpu.enable && config.airlock.guiGpu.startOnBoot;
  bootPrerequisites =
    [ "airlock-setup-config.service" ]
    ++ lib.optional guardedGui "airlock-start-gui-gpu.service"
    ++ lib.optional config.airlock.qubes.startNetworkProvidersOnBoot.enable
      "airlock-start-network-providers.service"
    ++ lib.optional config.airlock.qubes.deferredSetupConfigOnBoot.enable
      "airlock-setup-config-deferred.service";
in
{
  options.airlock.machineDeployment = {
    enable = lib.mkEnableOption "persistent repository-owned machine deployments";
    profile = lib.mkOption {
      type = lib.types.str;
      default = "default";
    };
    poolAdmission = {
      enable = lib.mkEnableOption "sys-dev guest artifact admission into the shared pool";
      caller = lib.mkOption {
        type = lib.types.str;
        default = "sys-dev";
        description = "Qubes domain allowed to submit guest pool artifacts.";
      };
      targets = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = "Guest domains for which the caller may submit pool artifacts.";
      };
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !poolAdmission.enable || poolAdmission.targets != [ ];
        message = "airlock.machineDeployment.poolAdmission.targets must not be empty when admission is enabled.";
      }
    ];
    environment.systemPackages = [ engine ] ++ lib.optional poolAdmission.enable poolArtifacts;
    environment.etc."airlock/machine-profile".text = cfg.profile + "\n";
    environment.etc."qubes-rpc/airlock.Pool.Admit" = lib.mkIf poolAdmission.enable {
      source = poolAdmissionHandler;
      mode = "0555";
    };
    virtualisation.qubes.packages = lib.optional poolAdmission.enable poolAdmissionPolicy;
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
            ExecStartPost = "${pkgs.systemd}/bin/journalctl --sync";
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
