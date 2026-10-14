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
    export PATH=${lib.makeBinPath [ pkgs.systemd ]}:"$PATH"
    exec ${pkgs.python3}/bin/python3 ${../../runtime/tools/sys-ctrl-guest-receive.py} ${action} \
      --engine ${engine}/bin/airlock-machine-deploy
  '';
  guestPlan = guestControl "plan";
  guestApply = guestControl "apply";
  machineControl = action: pkgs.writeShellScript "airlock-ctrl-machine-${action}" ''
    set -eu
    [ "$#" -eq 0 ] || exit 2
    export PATH=${lib.makeBinPath [ pkgs.nix pkgs.systemd ]}:"$PATH"
    exec ${pkgs.python3}/bin/python3 ${../../runtime/tools/sys-ctrl-machine-receive.py} ${action} \
      --engine ${engine}/bin/airlock-machine-deploy
  '';
  machinePrepare = machineControl "prepare";
  machinePlan = machineControl "plan";
  machineApply = machineControl "apply";
  reviewControl = pkgs.writeShellScript "airlock-review-control" ''
    set -eu
    [ "$#" -eq 0 ] || exit 2
    exec ${engine}/bin/airlock-machine-deploy review-control
  '';
  reviewHandler = pkgs.writeShellScript "airlock-review-control-qrexec" ''
    set -eu
    [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-ctrl ] || exit 1
    exec /run/wrappers/bin/sudo -n ${reviewControl}
  '';
  control = pkgs.writeShellScript "airlock-machine-control" ''
    set -eu
    [ "$#" -eq 0 ] || exit 2
    export PATH=${lib.makeBinPath [ pkgs.systemd ]}:"$PATH"
    exec ${pkgs.python3}/bin/python3 ${../../runtime/tools/sys-ctrl-machine-control.py}
  '';
  controlHandler = pkgs.writeShellScript "airlock-machine-control-qrexec" ''
    set -eu
    case "''${QREXEC_REMOTE_DOMAIN:-}" in usr-assistant|sys-ctrl) ;; *) exit 1 ;; esac
    exec /run/wrappers/bin/sudo -n ${control}
  '';
  guestHandler = action: helper: pkgs.writeShellScript "airlock-ctrl-guest-${action}-qrexec" ''
    set -eu
    [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-ctrl ] || exit 1
    exec /run/wrappers/bin/sudo -n ${helper}
  '';
  candidateAdmission = pkgs.writeShellScript "airlock-candidate-admission" ''
    set -eu
    [ "$#" -eq 1 ] || exit 2
    exec ${engine}/bin/airlock-machine-deploy admission-receive "$1" --caller sys-ctrl
  '';
  candidateHandler = pkgs.writeShellScript "airlock-candidate-admission-qrexec" ''
    set -eu
    [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-ctrl ] || exit 1
    exec /run/wrappers/bin/sudo -n ${candidateAdmission} "''${QREXEC_SERVICE_ARGUMENT:-}"
  '';
  guestPolicy = pkgs.writeTextDir "etc/qubes/policy.d/43-airlock-ctrl-guest.policy" ''
    airlock.Ctrl.MachineControl + usr-assistant dom0 allow
    airlock.Ctrl.MachineControl + sys-ctrl dom0 allow
    airlock.Ctrl.MachineControl * @anyvm @anyvm deny
    airlock.Ctrl.GuestDeploy * usr-assistant sys-ctrl allow
    airlock.Ctrl.GuestDeploy * @anyvm @anyvm deny
    airlock.Ctrl.ReviewStatus + usr-assistant sys-ctrl allow
    airlock.Ctrl.ReviewStatus * @anyvm @anyvm deny
    airlock.Ctrl.ReviewControl + sys-ctrl dom0 allow
    airlock.Ctrl.ReviewControl * @anyvm @anyvm deny
    airlock.Ctrl.GuestPlan * sys-ctrl dom0 allow
    airlock.Ctrl.GuestPlan * @anyvm @anyvm deny
    airlock.Ctrl.GuestApply * sys-ctrl dom0 allow
    airlock.Ctrl.GuestApply * @anyvm @anyvm deny
    airlock.Ctrl.MachinePlan * sys-ctrl dom0 allow
    airlock.Ctrl.MachinePlan * @anyvm @anyvm deny
    airlock.Ctrl.MachineApply * sys-ctrl dom0 allow
    airlock.Ctrl.MachineApply * @anyvm @anyvm deny
    airlock.Ctrl.MachinePrepare + sys-ctrl dom0 allow
    airlock.Ctrl.MachinePrepare * @anyvm @anyvm deny
    airlock.Pool.AdmitCandidate * sys-ctrl dom0 allow
    airlock.Pool.AdmitCandidate * @anyvm @anyvm deny
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
    userReview = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Require human approval of source and permitted impact before controller builds and activation.";
    };
    profile = lib.mkOption {
      type = lib.types.str;
      default = "default";
    };
    initialSource = lib.mkOption {
      type = lib.types.nullOr (lib.types.submodule {
        options = {
          revision = lib.mkOption { type = lib.types.strMatching "[0-9a-f]{40}"; };
          lockHash = lib.mkOption { type = lib.types.strMatching "[0-9a-f]{64}"; };
        };
      });
      default = null;
      description = "Machine source built into the initial image, independent of the parent repository's boot label.";
    };
    generationRetention.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Retain the newest two verified machine generations plus a rolling weekly anchor and aging successor; queue serialized pool GC after successful verification.";
    };
    retireDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Domains intentionally removed by this release and moved to recoverable trash.";
    };
    privateVolumeCopies = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "New VM names mapped to retired source VMs whose private volumes are copied once before startup.";
    };
    trashRetentionDays = lib.mkOption {
      type = lib.types.ints.between 1 3650;
      default = 30;
      description = "Minimum days before an unreferenced trashed domain is eligible for explicit purge.";
    };
  };
  config = lib.mkIf cfg.enable {
    # Physical GUI startup provisions only its dependencies. The remaining
    # topology is reconciled after the guarded desktop and input are ready.
    airlock.qubes.deferredSetupConfigOnBoot.enable = lib.mkIf guardedGui (lib.mkForce true);
    environment.etc."airlock/split-startup" = lib.mkIf guardedGui { text = "v1\n"; };
    environment.systemPackages = [ engine ];
    environment.etc."airlock/review-required" = lib.mkIf cfg.userReview { text = "v1\n"; };
    environment.etc."airlock/machine-source.json" = lib.mkIf (cfg.initialSource != null) {
      text = builtins.toJSON ({ schema = "airlock.machine-source.v1"; } // cfg.initialSource);
    };
    environment.etc."qubes-rpc/airlock.Ctrl.ReviewControl" = {
      source = reviewHandler;
      mode = "0555";
    };
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
    environment.etc."qubes-rpc/airlock.Ctrl.MachinePrepare" = {
      source = guestHandler "machine-prepare" machinePrepare;
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.Pool.AdmitCandidate" = {
      source = candidateHandler;
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.Ctrl.MachineControl" = {
      source = controlHandler;
      mode = "0555";
    };
    security.sudo.extraRules = [{
      users = [ config.airlock.dom0.user ];
      commands = [
        { command = "${reviewControl}"; options = [ "NOPASSWD" ]; }
        { command = "${control}"; options = [ "NOPASSWD" ]; }
        { command = "${guestPlan}"; options = [ "NOPASSWD" ]; }
        { command = "${guestApply}"; options = [ "NOPASSWD" ]; }
        { command = "${machinePlan}"; options = [ "NOPASSWD" ]; }
        { command = "${machineApply}"; options = [ "NOPASSWD" ]; }
        { command = "${machinePrepare}"; options = [ "NOPASSWD" ]; }
        { command = "${candidateAdmission}"; options = [ "NOPASSWD" ]; }
      ];
    }];
    environment.etc."airlock/machine-profile".text = cfg.profile + "\n";
    environment.etc."airlock/generation-retention.json".text = builtins.toJSON {
      enabled = cfg.generationRetention.enable;
      newest = 2;
      weeklyDays = 7;
      keepAgingSuccessor = true;
    };
    systemd.paths.airlock-generation-maintenance = lib.mkIf cfg.generationRetention.enable {
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathExists = "/var/lib/airlock/machine/maintenance/pending.json";
        Unit = "airlock-generation-maintenance.service";
      };
    };
    systemd.services = lib.mkMerge [
      (lib.mkIf cfg.generationRetention.enable {
        airlock-generation-maintenance = {
          description = "Prune obsolete verified machine generations and collect unused pool artifacts";
          after = [ "airlock-machine-autostart.service" ];
          restartIfChanged = false;
          stopIfChanged = false;
          unitConfig.StartLimitIntervalSec = 0;
          serviceConfig = {
            Type = "oneshot";
            ExecStart = "${engine}/bin/airlock-machine-deploy generation-maintenance";
            TimeoutStartSec = "infinity";
            Restart = "on-failure";
            RestartSec = "60s";
            Nice = 10;
            IOSchedulingClass = "best-effort";
            IOSchedulingPriority = 7;
          };
        };
      })
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
        airlock-setup-config.serviceConfig.ExecStart = lib.mkForce "${engine}/bin/airlock-machine-deploy boot-setup${lib.optionalString guardedGui " --gui"} ${setup}/bin/airlock-setup-config --apply${
          lib.optionalString (
            !guardedGui && config.airlock.qubes.setupConfigOnBoot.scope == "boot-critical"
          ) " --boot-critical"
        }";
      })
      (lib.mkIf config.airlock.qubes.deferredSetupConfigOnBoot.enable {
        airlock-setup-config-deferred.serviceConfig.ExecStart = lib.mkForce "${engine}/bin/airlock-machine-deploy boot-setup${lib.optionalString guardedGui " --background"} ${setup}/bin/airlock-setup-config --apply${lib.optionalString (!guardedGui) " --deferred"}";
      })
      (lib.mkIf guardedGui {
        airlock-gui-input-connect = lib.mkIf config.airlock.inputProxy.sender.enable {
          after = [ "airlock-start-gui-gpu.service" ];
        };
        airlock-setup-config-deferred = {
          requires = [ "airlock-start-gui-gpu.service" ]
            ++ lib.optional config.airlock.inputProxy.sender.enable "airlock-gui-input-connect.service";
          wants = lib.mkForce [
            "libvirtd.service"
            "qubes-db-dom0.service"
            "qubesd.service"
            "qubes-qrexec-policy-daemon.service"
          ];
          after = lib.mkForce ([
            "airlock-setup-config.service"
            "airlock-start-gui-gpu.service"
            "qubesd.service"
            "qubes-db-dom0.service"
            "qubes-qrexec-policy-daemon.service"
          ] ++ lib.optional config.airlock.inputProxy.sender.enable "airlock-gui-input-connect.service");
          before = [ "airlock-start-network-providers.service" "airlock-machine-autostart.service" ];
        };
        airlock-start-network-providers = lib.mkIf config.airlock.qubes.startNetworkProvidersOnBoot.enable {
          requires = [ "airlock-setup-config-deferred.service" ];
          after = [ "airlock-setup-config-deferred.service" ];
          before = lib.mkForce [ "airlock-machine-autostart.service" ];
          environment.AIRLOCK_QUBES_TOPOLOGY = "/run/airlock/startup/topology.json";
        };
      })
    ];
  };
}
