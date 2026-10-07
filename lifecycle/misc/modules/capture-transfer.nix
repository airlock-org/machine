{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.captureTransfer;
  helper = pkgs.writeScriptBin "airlock-capture-transfer" (
    "#!${pkgs.python3}/bin/python3\n" + builtins.replaceStrings
      [ "@qrexec@" "@xdgUserDir@" "@notifySend@" "@ffprobe@" "@gdbus@" "@gio@" ]
      [ "${pkgs.python3.pkgs.qubes-core-qrexec.domU}/bin/qrexec-client-vm"
        "${pkgs.xdg-user-dirs}/bin/xdg-user-dir"
        "${pkgs.libnotify}/bin/notify-send"
        "${pkgs.ffmpeg-headless}/bin/ffprobe"
        "${pkgs.glib}/bin/gdbus" "${pkgs.glib}/bin/gio" ]
      (builtins.readFile ./sys-gui/capture-transfer.py)
  );
  receiver = operation: pkgs.writeShellScript "airlock-capture-${operation}" ''
    set -eu
    case "''${QREXEC_SERVICE_ARGUMENT:-}" in image|video) ;; *) exit 2 ;; esac
    exec ${helper}/bin/airlock-capture-transfer ${operation} "$QREXEC_SERVICE_ARGUMENT"
  '';
in
{
  options.airlock.captureTransfer = {
    send.enable = lib.mkEnableOption "GNOME capture completion handoff";
    receive.enable = lib.mkEnableOption "receive captures from sys-gui";
    target = lib.mkOption {
      type = lib.types.strMatching "[A-Za-z][A-Za-z0-9_.-]*";
      default = "usr-home";
      description = "Personal VM receiving completed GNOME captures.";
    };
  };
  config = lib.mkMerge [
    (lib.mkIf (cfg.send.enable || cfg.receive.enable) {
      environment.systemPackages = [ helper ];
    })
    (lib.mkIf cfg.send.enable {
      nixpkgs.overlays = [ (_final: prev: {
        gnome-shell = prev.gnome-shell.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [ ../patches/gnome-shell/0001-completed-capture-handoff.patch ];
          postPatch = (old.postPatch or "") + ''
            substituteInPlace js/ui/screenshot.js js/dbusServices/screencast/screencastService.js \
              --replace-fail '@captureHelper@' '${helper}/bin/airlock-capture-transfer' \
              --replace-fail '@captureTarget@' '${cfg.target}'
          '';
        });
      }) ];
      # Runs once when the desktop starts. No polling timer or file watcher.
      environment.etc."xdg/autostart/airlock-capture-retry.desktop".text = ''
        [Desktop Entry]
        Type=Application
        Name=Deliver pending screen captures
        Exec=${helper}/bin/airlock-capture-transfer retry --target ${cfg.target}
        OnlyShowIn=GNOME;
        NoDisplay=true
      '';
    })
    (lib.mkIf cfg.receive.enable {
      environment.etc = {
        "qubes-rpc/airlock.CaptureImport" = { source = receiver "receive"; mode = "0555"; };
        "qubes-rpc/airlock.CaptureOpen" = { source = receiver "receive-open"; mode = "0555"; };
        "qubes/rpc-config/airlock.CaptureImport".text = "force-user='${config.airlock.qubeGuest.user}'\n";
        "qubes/rpc-config/airlock.CaptureOpen".text = "force-user='${config.airlock.qubeGuest.user}'\n";
      };
    })
  ];
}
