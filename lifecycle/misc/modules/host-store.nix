{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.hostStore;
  storage = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
  # Initramfs needs storage primitives, not the runtime CLI's wrapped package
  # closure (which also includes admission and filesystem creation tools).
  bootSource = lib.fileset.toSource {
    root = ../../runtime/tools/pkgs/airlock-qubes-storage/src;
    fileset = lib.fileset.fileFilter (file: file.hasExt "py")
      ../../runtime/tools/pkgs/airlock-qubes-storage/src;
  };
  lower = "local%3Froot%3D%2Fvar%2Flib%2Fairlock%2Fhost-store%2Flower%26read-only%3Dtrue";
  uri = "local-overlay://?lower-store=${lower}&upper-layer=/var/lib/airlock/host-store/active/upper";
in {
  imports = [ ./boot-diagnostics.nix ];
  options.airlock.hostStore.enable = lib.mkEnableOption "pool-backed dom0 Nix store";
  config = lib.mkIf cfg.enable {
    assertions = [{ assertion = config.boot.initrd.systemd.enable;
      message = "The host pool requires the systemd initramfs."; }];
    boot.supportedFilesystems = [ "erofs" "overlay" ];
    boot.initrd.kernelModules = [ "erofs" "overlay" "loop" "dm_mod" ];
    boot.initrd.systemd.storePaths = [ pkgs.python3 bootSource pkgs.nix pkgs.util-linux pkgs.lvm2 ];
    boot.initrd.systemd.services.airlock-host-store = {
      description = "Assemble selected immutable host pool before stage 2";
      requiredBy = [ "initrd-root-fs.target" ];
      after = [ "sysroot.mount" ];
      requires = [ "sysroot.mount" ];
      before = [ "initrd-root-fs.target" "initrd-nixos-activation.service" "initrd-find-nixos-closure.service" ];
      unitConfig.DefaultDependencies = false;
      path = [ pkgs.nix pkgs.util-linux pkgs.lvm2 ];
      environment.PYTHONPATH = toString bootSource;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        PrivateMounts = false;
        StandardOutput = "journal+console";
        StandardError = "journal+console";
        ExecStart = "${pkgs.python3}/bin/python -m airlock_qubes_storage.host_boot";
      };
    };
    # RequiresMountsFor=/sysroot/nix/store only waits for declared mount
    # units. Our overlay is mounted by a service, so it needs an explicit edge.
    boot.initrd.systemd.services.initrd-find-nixos-closure = lib.mkIf (!config.system.nixos-init.enable) {
      requires = [ "airlock-host-store.service" ];
      after = [ "airlock-host-store.service" ];
    };
    nix.settings.experimental-features = [ "local-overlay-store" "read-only-local-store" ];
    nix.settings.store = "daemon";
    # nix.conf selects the daemon for clients. Override it for the daemon
    # itself: NIX_REMOTE is only a default and would recurse into that socket.
    # systemd expands percent specifiers even in Environment= assignments.
    systemd.services.nix-daemon.environment.NIX_CONFIG =
      "store = ${lib.replaceStrings [ "%" ] [ "%%" ] uri}";
    environment.systemPackages = [ storage ];
    environment.etc."airlock/host-store".text = uri + "\n";
    # Mounts are made by stage 1 and survive switch-root. Never remount or
    # rebuild the composed store under running Nix/host processes.
    systemd.tmpfiles.rules = [ "d /var/lib/airlock/host-store 0700 root root - -" ];
  };
}
