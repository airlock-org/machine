{ config, lib, pkgs, ... }:
let
  setup = import ../../../../../runtime/tools/pkgs/airlock-setup { inherit pkgs; };
  bootNotification = pkgs.writeShellScriptBin "airlock-boot-notification" ''
    export PATH="${lib.makeBinPath [ pkgs.libnotify pkgs.python3.pkgs.qubes-core-admin-client ]}:$PATH"
    exec ${pkgs.python3}/bin/python3 ${../../../../../runtime/tools/pkgs/airlock-setup/boot-notification.py}
  '';
  launcher = pkgs.makeDesktopItem {
    name = "airlock-setup";
    desktopName = "AirlockOS Setup";
    exec = "${setup}/bin/airlock-setup";
    icon = "preferences-system";
    categories = [ "Settings" ];
  };
in
{
  config = lib.mkIf (config.airlock.sysGuiGuest.enable && config.airlock.sysGuiDesktop.session == "gnome") {
    environment.systemPackages = [ setup launcher ];
    environment.etc."xdg/autostart/airlock-boot-notification.desktop".text = ''
      [Desktop Entry]
      Type=Application
      Name=AirlockOS startup status
      Exec=${bootNotification}/bin/airlock-boot-notification
      OnlyShowIn=GNOME;
    '';
    environment.etc."xdg/autostart/airlock-setup.desktop".text = ''
      [Desktop Entry]
      Type=Application
      Name=AirlockOS Setup
      Exec=${setup}/bin/airlock-setup --auto
      OnlyShowIn=GNOME;
    '';
  };
}
