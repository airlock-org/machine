{ config, lib, pkgs, ... }:
let
  setup = import ../../../../../runtime/tools/pkgs/airlock-setup { inherit pkgs; };
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
    environment.etc."xdg/autostart/airlock-setup.desktop".text = ''
      [Desktop Entry]
      Type=Application
      Name=AirlockOS Setup
      Exec=${setup}/bin/airlock-setup --auto
      OnlyShowIn=GNOME;
      X-GNOME-Autostart-Phase=Desktop
    '';
  };
}
