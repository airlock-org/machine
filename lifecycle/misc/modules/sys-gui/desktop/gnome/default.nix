{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.airlock.sysGuiDesktop;
  hostPoweroff = pkgs.writeShellScript "airlock-gnome-host-poweroff" ''
    exec ${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-run \
      --pass-io --no-gui --service dom0 airlock.Dom0Poweroff
  '';
  hostPoweroffExtension = pkgs.stdenvNoCC.mkDerivation {
    pname = "airlock-gnome-host-poweroff";
    version = "1";
    passthru = { inherit hostPoweroff; };
    src = ./host-poweroff-extension;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      extension_dir="$out/share/gnome-shell/extensions/airlock-host-poweroff@airlock.local"
      mkdir -p "$extension_dir"
      cp extension.js metadata.json "$extension_dir/"
      substituteInPlace "$extension_dir/extension.js" \
        --replace-fail '@hostPoweroff@' '${hostPoweroff}'
      runHook postInstall
    '';
  };
  identityPython = pkgs.python3.withPackages (ps: [ ps.python-xlib ]);
  identityReader = pkgs.writeScript "airlock-focused-vm-identity" (
    "#!${identityPython}/bin/python3\n"
    + builtins.replaceStrings [ "@guid@" ] [ "${pkgs.qubes-gui-daemon}/bin/qubes-guid" ] (
      builtins.readFile ./focused-vm-extension/identity.py
    )
  );
  focusedVMExtension = pkgs.stdenvNoCC.mkDerivation {
    pname = "airlock-gnome-focused-vm";
    version = "1";
    src = ./focused-vm-extension;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      extension_dir="$out/share/gnome-shell/extensions/airlock-focused-vm@airlock.local"
      mkdir -p "$extension_dir"
      cp extension.js state.js metadata.json stylesheet.css "$extension_dir/"
      substituteInPlace "$extension_dir/extension.js" \
        --replace-fail '@identityReader@' '${identityReader}'
      runHook postInstall
    '';
    passthru = { inherit identityReader; };
  };
  vmSearchExtension = pkgs.stdenvNoCC.mkDerivation {
    pname = "airlock-gnome-vm-search";
    version = "1";
    src = ./vm-search-extension;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      extension_dir="$out/share/gnome-shell/extensions/airlock-vm-search@airlock.local"
      mkdir -p "$extension_dir"
      cp extension.js model.js metadata.json stylesheet.css "$extension_dir/"
      runHook postInstall
    '';
  };
  vmWorkspacesExtension = pkgs.stdenvNoCC.mkDerivation {
    pname = "airlock-gnome-vm-workspaces";
    version = "1";
    src = ./vm-workspaces-extension;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      extension_dir="$out/share/gnome-shell/extensions/airlock-vm-workspaces@airlock.local"
      mkdir -p "$extension_dir"
      cp extension.js model.js metadata.json stylesheet.css "$extension_dir/"
      cp ${./focused-vm-extension/state.js} "$extension_dir/identity.js"
      cp ${./vm-search-extension/model.js} "$extension_dir/inventory.js"
      substituteInPlace "$extension_dir/extension.js" \
        --replace-fail '@identityReader@' '${identityReader}'
      runHook postInstall
    '';
  };
  enabledExtensions = [
    "airlock-host-poweroff@airlock.local"
    "airlock-focused-vm@airlock.local"
    "airlock-vm-search@airlock.local"
    "airlock-vm-workspaces@airlock.local"
  ];
  enabledExtensionsGVariant = "[${
    lib.concatMapStringsSep "," (extension: "'${extension}'") enabledExtensions
  }]";
in
{
  imports = [ ./setup.nix ../../../capture-transfer.nix ];
  config = lib.mkIf (cfg.session == "gnome") {
    airlock.captureTransfer.send.enable = lib.mkDefault (
      config.airlock.sysGuiGuest.defaultApplicationVM != null
    );
    # Early login can precede the host's Admin API readiness. The upstream
    # desktop autostart exits after a refused request and leaves every app VM
    # waiting for its GUI connection. Supervise the same upstream watcher.
    environment.etc."xdg/autostart/qvm-start-daemon.desktop".text = ''
      [Desktop Entry]
      Type=Application
      Name=Qubes Guid/Pacat
      Hidden=true
    '';
    systemd.user.services.airlock-gui-daemons = {
      description = "Forward windows from VMs assigned to this GUI session";
      wantedBy = [ "graphical-session.target" ];
      after = [ "graphical-session-pre.target" ];
      partOf = [ "graphical-session.target" ];
      unitConfig.StartLimitIntervalSec = 0;
      # The upstream event reader executes qrexec-client-vm by name.
      path = [
        pkgs.coreutils
        pkgs.qubes-gui-daemon
        pkgs.python3.pkgs.qubes-core-admin-client
        pkgs.python3.pkgs.qubes-core-qrexec.domU
      ];
      serviceConfig = {
        ExecStart = "${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-start-daemon --all --watch";
        Restart = "always";
        RestartSec = "5s";
      };
    };
    # Applications own their title bars. The trusted GUI daemon adds a thin
    # VM-coloured outline for every forwarded VM window; the focused VM
    # panel identifies every VM. Decoration policy never uses application class.
    nixpkgs.overlays = lib.mkAfter [
      (_final: prev: {
        qubes-gui-daemon = prev.qubes-gui-daemon.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [
            ../../../../patches/qubes-gui-daemon/0003-gui-daemon-leave-window-chrome-to-applications.patch
          ];
        });
      })
    ];
    # GNOME's greeter and screen locking require GDM. Its NixOS module
    # disables the common sys-gui LightDM default.
    services.displayManager.gdm = {
      enable = true;
      # The GUI VM must stay awake while waiting for a login.
      autoSuspend = false;
    };
    services.desktopManager.gnome = {
      enable = true;
      extraGSettingsOverridePackages = [
        # The override below changes org.gnome.shell. DING's own schema lives
        # inside its extension directory, rather than NixOS's standard schema
        # location, so it must not be passed to the override compiler.
        pkgs.gnome-shell
      ];
      extraGSettingsOverrides = ''
        [org.gnome.shell]
        enabled-extensions=${enabledExtensionsGVariant}
      '';
    };
    # GNOME Shell offers gnome-tour on first login when its desktop entry is
    # installed. AirlockOS Setup owns that first-run welcome flow instead.
    environment.gnome.excludePackages = [ pkgs.gnome-tour ];
    # Connected desktop profiles run applications in their chosen VM. Minimal
    # profiles retain GNOME's local core apps; both discover attached app VMs.
    services.gnome.core-apps.enable = lib.mkForce (
      config.airlock.sysGuiGuest.defaultApplicationVM == null
    );
    environment.systemPackages = [
      hostPoweroffExtension
      focusedVMExtension
      vmSearchExtension
      vmWorkspacesExtension
      # Accessibility controls the sys-gui session itself.
      pkgs.gnome-tecla
    ];

    # Sys-gui has no NetVM and should not acquire host-facing services merely
    # because the desktop implementation normally enables them by default.
    networking.networkmanager.enable = lib.mkForce false;
    hardware.bluetooth.enable = lib.mkForce false;
    services.avahi.enable = lib.mkForce false;
    services.gnome.gnome-initial-setup.enable = lib.mkForce false;
    services.gnome.gnome-remote-desktop.enable = lib.mkForce false;
    services.gnome.gnome-user-share.enable = lib.mkForce false;
    services.gnome.rygel.enable = lib.mkForce false;

  };
}
