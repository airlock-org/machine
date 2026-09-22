{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.airlock.bootSupervisor;
  supervisor = pkgs.callPackage ../../runtime/tools/pkgs/airlock-boot-supervisor { };
  recoveryConsole = pkgs.writeShellScript "airlock-boot-recovery-console" ''
    set -eu
    ${supervisor}/bin/airlock-boot-supervisor recovery-ack
    ${pkgs.ncurses}/bin/clear
    echo
    echo "  Airlock | Safe boot recovery"
    echo
    echo "  The previous boot stopped before all required VMs became ready."
    echo "  PCI-backed VMs and GPU handoff are disabled for this boot."
    echo
    echo "  Persistent evidence: /var/log/airlock/boot-attempts"
    echo "  Attempt state:      /var/lib/airlock/boot-attempts"
    echo "  Failed units:       systemctl --failed"
    echo
    echo "  Log in below, inspect the evidence, then reboot to make one new attempt."
    echo
    exec ${pkgs.util-linux}/bin/agetty --noclear tty1 linux
  '';
  failureConsole = pkgs.writeShellScript "airlock-boot-failure-console" ''
    set -eu
    ${pkgs.ncurses}/bin/clear
    boot_id="$(${pkgs.coreutils}/bin/tr -d '\n' </proc/sys/kernel/random/boot_id)"
    echo
    echo "  Airlock | Boot failed"
    echo
    echo "  A required VM did not become ready. Airlock has not accepted this boot."
    echo "  The failed stage and diagnostics have been saved."
    echo
    echo "  Evidence:     /var/log/airlock/boot-attempts/$boot_id"
    echo "  Attempt state: /var/lib/airlock/boot-attempts/$boot_id/state.json"
    echo "  Failed units:  systemctl --failed"
    echo
    echo "  Log in below to inspect the failure."
    echo
    exec ${pkgs.util-linux}/bin/agetty --noclear tty1 linux
  '';
  gate = "${pkgs.coreutils}/bin/test ! -e /run/airlock/boot-supervisor/recovery-required.json";
  failureTarget = [ "airlock-boot-failure@%n.service" ];
in
{
  options.airlock.bootSupervisor.enable = lib.mkEnableOption "persistent guarded VM boot supervision";

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ supervisor ];
    # logind starts autovt@tty1 independently of getty@tty1. Reserve both
    # names so bootstrap and its diagnostic console have exclusive tty1 use.
    systemd.services."getty@tty1".enable = false;
    systemd.services."autovt@tty1".enable = false;

    systemd.services.airlock-boot-attempt-prepare = {
      description = "Recover an interrupted Airlock boot before VM logs are reopened";
      wantedBy = [ "multi-user.target" ];
      before = [
        "xenconsoled.service"
        "qubesd.service"
        "airlock-setup-config.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${supervisor}/bin/airlock-boot-supervisor prepare";
      };
    };

    systemd.services.xenconsoled = {
      requires = [ "airlock-boot-attempt-prepare.service" ];
      after = [ "airlock-boot-attempt-prepare.service" ];
    };
    systemd.services.qubesd = {
      requires = [ "airlock-boot-attempt-prepare.service" ];
      after = [ "airlock-boot-attempt-prepare.service" ];
    };

    systemd.services."airlock-boot-failure@" = {
      description = "Capture Airlock boot failure for %i";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${supervisor}/bin/airlock-boot-supervisor fail unit:%i 'systemd boot stage failed'";
        # A diagnostic capture error must not suppress the login console.
        ExecStopPost = "${pkgs.systemd}/bin/systemctl --no-block start airlock-boot-failure-console.service";
        TimeoutStartSec = 90;
      };
    };

    systemd.services.airlock-boot-failure-console = {
      description = "Show an actionable console after a supervised boot failure";
      conflicts = [ "airlock-boot-recovery-console.service" ];
      after = [
        "airlock-setup-config.service"
        "airlock-boot-recovery-console.service"
      ];
      serviceConfig = {
        Type = "idle";
        ExecStart = failureConsole;
        Restart = "always";
        RestartSec = 1;
        StandardInput = "tty";
        StandardOutput = "tty";
        StandardError = "tty";
        TTYPath = "/dev/tty1";
        TTYReset = true;
        TTYVHangup = true;
      };
    };

    systemd.services.airlock-boot-recovery-console = {
      description = "Keep an interrupted Airlock boot on a diagnostic console";
      wantedBy = [ "multi-user.target" ];
      after = [ "airlock-boot-attempt-prepare.service" ];
      unitConfig.ConditionPathExists = "/run/airlock/boot-supervisor/recovery-required.json";
      serviceConfig = {
        Type = "idle";
        ExecStart = recoveryConsole;
        Restart = "always";
        RestartSec = 1;
        StandardInput = "tty";
        StandardOutput = "tty";
        StandardError = "tty";
        TTYPath = "/dev/tty1";
        TTYReset = true;
        TTYVHangup = true;
      };
    };

    systemd.services.airlock-setup-config = {
      after = [ "airlock-boot-attempt-prepare.service" ];
      unitConfig.OnFailure = failureTarget;
      serviceConfig.ExecCondition = [ gate ];
      serviceConfig.ExecStartPre = [
        "${supervisor}/bin/airlock-boot-supervisor stage topology 'configuring declared topology'"
      ];
    };
    systemd.services.airlock-start-network-providers = {
      unitConfig.OnFailure = failureTarget;
      serviceConfig.ExecCondition = [ gate ];
      serviceConfig.TimeoutStartSec = 300;
    };
    systemd.services.airlock-start-gui-gpu = {
      unitConfig.OnFailure = failureTarget;
      serviceConfig.ExecCondition = [ gate ];
      serviceConfig.TimeoutStartSec = 300;
    };
    systemd.services.airlock-machine-autostart = {
      unitConfig.OnFailure = failureTarget;
      serviceConfig.ExecCondition = [ gate ];
      serviceConfig.TimeoutStartSec = 600;
    };
  };
}
