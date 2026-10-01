{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.qubeGuest;
  tool = pkgs.writeShellScript "sys-agent-build-store" ''
    exec ${pkgs.python3}/bin/python3 ${../../runtime/tools/scripts/sys-agent-build-store.py} \
      "$@" --domain ${lib.escapeShellArg cfg.role} --user ${lib.escapeShellArg cfg.user} --nix-store ${pkgs.nix}/bin/nix-store
  '';
  devices = [ "dev-xvdg.device" "dev-xvdh.device" ];
in
{
  airlock.qubeGuest.configNixStoreRoot = lib.mkDefault "/rw/airlock-builder/merged";
  assertions = [{
    assertion = cfg.configNixStoreRoot == "/rw/airlock-builder/merged";
    message = "the builder's persistent pool-backed store must use /rw/airlock-builder/merged";
  }];
  boot.supportedFilesystems = [ "erofs" "overlay" ];
  nix.settings.experimental-features = [ "local-overlay-store" "read-only-local-store" ];

  systemd.services."airlock-${cfg.role}-build-store" = {
    description = "Mount ${cfg.role}'s pinned pool base and persistent private build layer";
    wantedBy = [ "multi-user.target" ];
    requires = [ "airlock-qubes-rw-init.service" ] ++ devices;
    after = [ "airlock-qubes-rw-init.service" ] ++ devices;
    before = [ "qubes-qrexec-agent.service" ];
    path = [ pkgs.coreutils pkgs.util-linux ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${tool} start";
      ExecStop = "${tool} stop";
      TimeoutStartSec = 120;
      # Mounts must be visible to the normal unprivileged build processes.
      PrivateMounts = false;
    };
  };
}
