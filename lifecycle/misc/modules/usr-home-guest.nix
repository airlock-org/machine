{ lib, pkgs, ... }:
{
  imports = [ ./app-gui-guest.nix ./capture-transfer.nix ];
  airlock.captureTransfer.receive.enable = true;

  # Personal applications get the ordinary NixOS user environment. All other
  # guests retain their current minimal defaults and role-specific packages.
  airlock.minimalGuest.enable = lib.mkForce false;
  system.etc.overlay.enable = true;
  services.userborn.enable = true;

  # Reuse our pinned upstream application selection, including GNOME Web,
  # without starting another GNOME Shell, display manager or hardware manager.
  services.gnome = {
    core-apps.enable = true;
    at-spi2-core.enable = true;
    evolution-data-server.enable = true;
    glib-networking.enable = true;
    gnome-keyring.enable = true;
    gnome-online-accounts.enable = true;
    localsearch.enable = true;
    tinysparql.enable = true;
  };
  programs.dconf.enable = true;
  fonts.packages = [ pkgs.adwaita-fonts ];
  # The Qubes X server is supplied by the GUI agent, so NixOS's ordinary
  # X-server switch does not enable Mesa/EGL here. WebKit still needs those
  # runtime drivers for software rendering; this grants no GPU assignment.
  hardware.graphics.enable = true;
  services.gvfs.enable = true;
  security.pam.services.qubes-gui-agent.enableGnomeKeyring = true;

  environment.systemPackages = with pkgs; [
    firefox # Additional browser; GNOME Web remains the default
    glib # gio/gsettings for MIME activation and application preferences
    gtk3 # gtk-launch for standard desktop entry activation
    glycin-thumbnailer
    gst-thumbnailers
    xdg-user-dirs
    xdg-user-dirs-gtk
  ];

  # AppVM sessions use X-QUBES, so use the GTK portal instead of a portal that
  # depends on GNOME Shell in this VM. Secret storage uses the stock keyring.
  xdg.portal = {
    enable = true;
    extraPortals = [ pkgs.xdg-desktop-portal-gtk ];
    config.common = {
      default = [ "gtk" ];
      "org.freedesktop.impl.portal.Secret" = [ "gnome-keyring" ];
    };
  };

  xdg.mime.defaultApplications = {
    "x-scheme-handler/http" = "org.gnome.Epiphany.desktop";
    "x-scheme-handler/https" = "org.gnome.Epiphany.desktop";
    "text/html" = "org.gnome.Epiphany.desktop";
    "application/xhtml+xml" = "org.gnome.Epiphany.desktop";
    "image/png" = "org.gnome.Loupe.desktop";
    "image/jpeg" = "org.gnome.Loupe.desktop";
    "image/webp" = "org.gnome.Loupe.desktop";
    "image/gif" = "org.gnome.Loupe.desktop";
    "image/svg+xml" = "org.gnome.Loupe.desktop";
    "application/pdf" = "org.gnome.Papers.desktop";
    "text/plain" = "org.gnome.TextEditor.desktop";
    "inode/directory" = "org.gnome.Nautilus.desktop";
  };

  # Applications are deployed from the declared machine configuration. Keep
  # the interactive conveniences, but leave package installation to sys-ctrl.
  nix.enable = false;
  nix.channel.enable = false;
  system.disableInstallerTools = true;
  services.flatpak.enable = false;
}
