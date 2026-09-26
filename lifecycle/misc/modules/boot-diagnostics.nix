{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.bootDiagnostics;
  recorder = ../tools/scripts/boot-journal.py;
in {
  options.airlock.bootDiagnostics.enable = lib.mkEnableOption "persistent development boot diagnostics";
  config = lib.mkIf cfg.enable {
    boot.consoleLogLevel = lib.mkForce 7;
    boot.kernelParams = [ "systemd.show_status=1" "rd.systemd.show_status=1" ];
    boot.initrd.verbose = true;
    # Bare paths are interpreted as submodule imports by this option's type.
    # A store-path string includes the script as data instead.
    boot.initrd.systemd.storePaths = [ pkgs.python3 "${recorder}" ];
    boot.initrd.systemd.services.airlock-boot-journal = {
      description = "Save initramfs journal to the root filesystem";
      wantedBy = [ "initrd.target" ];
      after = [ "sysroot.mount" "systemd-journald.service" ];
      requires = [ "sysroot.mount" ];
      before = [ "airlock-host-store.service" "initrd-find-nixos-closure.service" ];
      unitConfig = { DefaultDependencies = false; IgnoreOnIsolate = true; };
      serviceConfig = {
        Type = "simple";
        ExecStart = "${pkgs.python3}/bin/python ${recorder} ${config.boot.initrd.systemd.package}/bin/journalctl";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };
    # The early recorder captures stage 1; journald owns stage 2 and the
    # handoff's /run journal. Keep storage bounded on development USB media.
    services.journald.settings.Journal = {
      Storage = "persistent";
      SystemMaxUse = lib.mkDefault "128M";
      SyncIntervalSec = lib.mkDefault "5s";
    };
    systemd.tmpfiles.rules = [ "d /var/log/airlock-boot 0700 root root - -" ];
  };
}
