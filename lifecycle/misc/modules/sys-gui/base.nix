{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.airlock.sysGuiGuest;
  guiAgent = pkgs.qubes-gui-agent-linux;
  guestUser = config.airlock.qubeGuest.user;
  desktopCommon = pkgs.python3.pkgs.qubes-desktop-linux-common.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ../../patches/qubes-desktop-linux-common/0001-discover-application-vms.patch
      ../../patches/qubes-desktop-linux-common/0002-preserve-application-icon-colours.patch
    ];
  });
  hybrid = cfg.displayMode == "hybrid";
  physicalXserver = pkgs.writeShellScript "airlock-gui-physical-xserver" ''
    export LD_PRELOAD=${pkgs.qubes-gui-daemon}/lib/qubes-gui-daemon/shmoverride.so
    exec ${pkgs.xorg-server}/bin/Xorg "$@"
  '';
  appLauncher = pkgs.writeShellScriptBin "airlock-launch-qube-app" ''
    set -u

    label="$1"
    target="$2"
    service="''${3:-}"
    log="$(${pkgs.coreutils}/bin/mktemp --tmpdir="''${XDG_RUNTIME_DIR:-/tmp}" airlock-app-launch.XXXXXX)"
    trap '${pkgs.coreutils}/bin/rm -f "$log"' EXIT

    # Only readiness is retried: a control-service restart during boot must
    # not reject the user's launch, and StartApp must never be submitted twice.
    readiness_deadline=$((SECONDS + 90))
    ready_status=124
    while [ "$SECONDS" -lt "$readiness_deadline" ]; do
      remaining=$((readiness_deadline - SECONDS))
      if readiness_service="$(${pkgs.coreutils}/bin/timeout --foreground \
        --signal=TERM --kill-after=5s "$remaining" ${appmenusPython}/bin/python3 -c '
import qubesadmin, sys
vm = qubesadmin.Qubes().domains[sys.argv[1]]
print("airlock.AppSessionReady" if vm.features.get("supported-rpc.airlock.AppSessionReady", False) else "qubes.WaitForSession")
' "$target" 2>>"$log")"; then
        remaining=$((readiness_deadline - SECONDS))
        [ "$remaining" -gt 0 ] || break
        ${pkgs.coreutils}/bin/timeout --foreground --signal=TERM --kill-after=5s "$remaining" \
          ${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-run \
          --auto --gui --pass-io --service "$target" "$readiness_service" \
          >>"$log" 2>&1
        ready_status=$?
        [ "$ready_status" -ne 0 ] || break
      fi
      ready_status=124
      printf 'Session readiness interrupted; retrying within the launch deadline.\n' >>"$log"
      [ "$((readiness_deadline - SECONDS))" -gt 2 ] || break
      ${pkgs.coreutils}/bin/sleep 2
    done
    if [ "$ready_status" -ne 0 ]; then
      ${pkgs.systemd}/bin/systemd-cat -t airlock-app-launch <"$log" || true
      if [ "$ready_status" -eq 124 ]; then
        detail="$target did not become ready within 90 seconds."
      else
        detail="$target could not start its graphical session."
      fi
      ${pkgs.libnotify}/bin/notify-send --app-name=Airlock \
        "Could not start $label" "$detail" || true
      exit "$ready_status"
    fi

    [ -n "$service" ] || exit 0
    workspace_token=""
    ${lib.optionalString ((config.airlock.sysGuiDesktop.session or "xfce") == "gnome") ''
      if ! workspace_token="$(${workspaceClient} prepare "$target" 2>"$log")"; then
        ${pkgs.systemd}/bin/systemd-cat -t airlock-app-launch <"$log" || true
        ${pkgs.libnotify}/bin/notify-send --app-name=Airlock \
          "Could not start $label" "$target could not prepare its workspace." || true
        exit 1
      fi
    ''}
    ${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-run \
      --auto --gui --service "$target" "$service" >"$log" 2>&1
    app_status=$?
    if [ "$app_status" -ne 0 ]; then
      ${pkgs.systemd}/bin/systemd-cat -t airlock-app-launch <"$log" || true
      if [ -n "$workspace_token" ]; then
        ${workspaceClient} cancel "$workspace_token" >>"$log" 2>&1 || true
      fi
      ${pkgs.libnotify}/bin/notify-send --app-name=Airlock \
        "Could not start $label" \
        "$target rejected the application request." || true
    fi
    exit "$app_status"
  '';
  syncAppmenus = pkgs.writeShellScriptBin "airlock-sync-qube-appmenus" ''
    set -eu
    [ "$#" -eq 1 ] || {
      echo 'Usage: airlock-sync-qube-appmenus VM' >&2
      exit 2
    }
    # Explicitly start the selected VM, even if its boot autostart is disabled.
    ${appLauncher}/bin/airlock-launch-qube-app Applications "$1"
    unset QREXEC_REMOTE_DOMAIN
    exec ${pkgs.coreutils}/bin/timeout --signal=TERM --kill-after=5s 90s \
      ${applicationMenus}/bin/airlock-application-menus --sync "$1" --force
  '';
  appmenusPython = pkgs.python3.withPackages (ps: [ desktopCommon ps.qubes-core-admin-client ]);
  workspaceClient = pkgs.writeScript "airlock-prepare-workspace" (
    "#!${appmenusPython}/bin/python3\n"
    + builtins.replaceStrings [ "@gdbus@" ] [ "${pkgs.glib}/bin/gdbus" ]
      (builtins.readFile ./workspace-launch.py)
  );
  applicationMenus = pkgs.writeShellScriptBin "airlock-application-menus" ''
    export PATH="${lib.makeBinPath [ pkgs.xdg-utils pkgs.desktop-file-utils pkgs.graphicsmagick ]}:$PATH"
    exec ${appmenusPython}/bin/python3 ${./application-menus.py} "$@"
  '';
  receiveAppmenus = pkgs.writeShellScript "airlock-receive-qube-appmenus" ''
    set -eu
    [ -z "''${QREXEC_SERVICE_ARGUMENT:-}" ] || exit 2
    exec ${pkgs.coreutils}/bin/timeout --signal=TERM --kill-after=5s 90s \
      ${applicationMenus}/bin/airlock-application-menus --receive
  '';
  # Web-link routing is a desktop default, independent of the installed browser.
  # The target VM resolves its own default handler through the normal MIME rules.
  openUrlLauncher = pkgs.writeShellScriptBin "airlock-open-url" ''
    set -eu
    [ "$#" -eq 1 ] || exit 2
    exec ${appLauncher}/bin/airlock-launch-qube-app "Web link" \
      ${lib.escapeShellArg (if cfg.defaultApplicationVM == null then "" else cfg.defaultApplicationVM)} "airlock.OpenUrl+$1"
  '';
  openUrlDesktop = pkgs.makeDesktopItem {
    name = "airlock-open-url";
    desktopName = "Open web link";
    exec = "airlock-open-url %u";
    icon = "web-browser";
    noDisplay = true;
    mimeTypes = [ "x-scheme-handler/http" "x-scheme-handler/https" ];
  };
  sysAgentTailscaleEnrollment = pkgs.writeShellScript "airlock-start-sys-agent-tailscale" ''
    set -eu
    exec ${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-run \
      --pass-io --no-gui --service sys-agent airlock.DevTailscaleUp
  '';
  sysAgentTailscaleLauncher = pkgs.writeShellScriptBin "airlock-start-sys-agent-tailscale" ''
    exec ${pkgs.xterm}/bin/xterm -hold -fa 'DejaVu Sans Mono' -fs 16 -geometry 100x30 -T 'Start sys-agent Tailscale' \
      -e ${sysAgentTailscaleEnrollment}
  '';
  sysAgentTailscaleDesktop = pkgs.makeDesktopItem {
    name = "airlock-start-sys-agent-tailscale";
    desktopName = "Start sys-agent Tailscale";
    comment = "Enroll sys-agent in Tailscale and display the login URL";
    exec = "airlock-start-sys-agent-tailscale";
    icon = "network-workgroup";
    categories = [ "System" "Network" ];
  };
  # LightDM owns the inner display and authenticates the desktop user. The
  # Qubes agent still owns the outer Xorg display (:1) forwarded to dom0.
  loginXserver = pkgs.writeShellScript "airlock-gui-login-xserver" ''
    set -euo pipefail
    export DISPLAY=:1
    unset XAUTHORITY
    ${pkgs.coreutils}/bin/timeout 60 ${pkgs.bash}/bin/bash -c '
      until ${pkgs.xdpyinfo}/bin/xdpyinfo -display :1 >/dev/null 2>&1; do
        ${pkgs.coreutils}/bin/sleep 1
      done
    '

    # NixOS/LightDM adds Xorg logging and physical-seat options that Xephyr
    # does not implement. Preserve display selection, -auth and access control.
    args=()
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -logfile|-seat) shift 2 ;;
        vt[0-9]*|-novtswitch) shift ;;
        *) args+=("$1"); shift ;;
      esac
    done
    export LD_PRELOAD=${pkgs.qubes-gui-daemon}/lib/qubes-gui-daemon/shmoverride.so
    exec ${pkgs.xorg-server}/bin/Xephyr \
      -sw-cursor -no-host-grab -output DUMMY0 "''${args[@]}"
  '';
  guiSession = pkgs.writeShellScript "airlock-gui-login-session" ''
    set -euo pipefail
    if [ "''${QREXEC_REMOTE_DOMAIN:-}" != dom0 ]; then
      echo "GUI login may only be started by dom0" >&2
      exit 1
    fi
    exec 9>/run/airlock-gui-login.lock
    ${pkgs.util-linux}/bin/flock -n 9 || exit 1
    trap '${pkgs.systemd}/bin/systemctl stop --no-block display-manager.service' EXIT
    trap 'exit 0' HUP INT TERM
    ${pkgs.systemd}/bin/systemctl start display-manager.service
    # Logging out returns to the guest greeter, so the relay stays connected.
    while ${pkgs.systemd}/bin/systemctl is-active --quiet display-manager.service; do
      ${pkgs.coreutils}/bin/sleep 2
    done
    ${pkgs.systemd}/bin/journalctl -u display-manager.service -n 30 --no-pager >&2
    exit 1
  '';
  # XFCE's panel power action calls `xfce4-session-logout --halt`. In a GUI
  # domain, upstream would halt only this VM, which destroys the active
  # desktop relay. Route precisely that action through the dedicated dom0
  # qrexec service; all other session actions retain XFCE's normal behavior.
  poweroffSessionAction = lib.hiPrio (pkgs.writeShellScriptBin "xfce4-session-logout" ''
    set -eu
    if [ "''${1:-}" = "--halt" ]; then
      exec ${pkgs.python3.pkgs.qubes-core-admin-client}/bin/qvm-run \
        --pass-io --no-gui --service dom0 airlock.Dom0Poweroff
    fi
    exec ${pkgs.xfce4-session}/bin/xfce4-session-logout "$@"
  '');
in
{
  imports = [ ../qubes-input-proxy.nix ../dev-dom0-terminal.nix ];
  options.airlock.sysGuiGuest.enable = lib.mkEnableOption "the Airlock hybrid sys-gui role";
  options.airlock.sysGuiGuest.displayMode = lib.mkOption {
    type = lib.types.enum [ "hybrid" "physical-amd-lab" ];
    default = "hybrid";
    description = "Hybrid relay by default; physical AMD Xorg is an experimental lab mode.";
  };
  options.airlock.sysGuiGuest.physicalDesktopOnBoot = lib.mkEnableOption
    "automatic physical desktop login after guest boot (not the hybrid relay)";
  options.airlock.sysGuiGuest.enableXfce = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = "install the XFCE session and its Airlock-specific integration";
  };
  options.airlock.sysGuiGuest.applicationMenus.enable = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = "Discover applications in every VM assigned to this GUI VM.";
  };
  options.airlock.sysGuiGuest.defaultApplicationVM = lib.mkOption {
    type = lib.types.nullOr lib.types.str;
    default = "usr-home";
    description = "VM whose default browser handles web links; null for local desktop applications.";
  };
  options.airlock.sysGuiGuest.sysAgentTailscaleShortcut.enable = lib.mkEnableOption
    "the desktop shortcut that enrolls sys-agent in Tailscale";

  config = lib.mkIf cfg.enable {
    airlock.inputProxy.receiver.enable = lib.mkDefault (!hybrid);
    # Keep a text-only physical diagnostic mode available. graphical.target
    # itself pulls in LightDM, independently of the service's wantedBy.
    systemd.defaultUnit = lib.mkIf (!hybrid) (lib.mkForce (
      if cfg.physicalDesktopOnBoot then "graphical.target" else "multi-user.target"
    ));
    boot.kernelModules = [
      "xen-gntalloc"
      "xen-gntdev"
      "xen-evtchn"
    ];

    environment.systemPackages = [
      guiAgent
      pkgs.qubes-gui-daemon
      pkgs.qubes-manager
      pkgs.qubes-artwork
      pkgs.python3.pkgs.qubes-core-admin-client
      desktopCommon
      pkgs.python3.pkgs.qubes-desktop-linux-manager
      pkgs.xdpyinfo
      pkgs.xinit
      pkgs.xorgserver
      pkgs.xdotool
      pkgs.xterm
      appLauncher
      syncAppmenus
    ]
    ++ lib.optionals cfg.enableXfce [
      pkgs.xfce4-session
      poweroffSessionAction
      pkgs.xfce4-panel
      pkgs.xfdesktop
    ]
    ++ lib.optional cfg.applicationMenus.enable applicationMenus
    ++ lib.optionals (cfg.defaultApplicationVM != null) [ openUrlLauncher openUrlDesktop ]
    ++ lib.optionals cfg.sysAgentTailscaleShortcut.enable [
      sysAgentTailscaleLauncher
      sysAgentTailscaleDesktop
    ];

    services.udev.packages = [ guiAgent ];
    services.xserver = {
      enable = true;
      desktopManager.xfce.enable = cfg.enableXfce;
      desktopManager.xterm.enable = false;
      displayManager = {
        xserverBin = lib.mkOverride 50 (if hybrid then loginXserver else physicalXserver);
        xserverArgs = lib.mkIf hybrid (lib.mkForce [ ]);
        lightdm = {
          enable = lib.mkDefault true;
          extraConfig = lib.mkIf hybrid ''
            logind-check-graphical=false
            minimum-vt=8
          '';
          extraSeatDefaults = lib.mkIf hybrid ''
            type=xlocal
            xserver-share=true
          '';
        };
      };
    };
    services.displayManager = {
      defaultSession = "xfce";
      autoLogin.enable = false;
    };
    systemd.services.display-manager = {
      # Hybrid always waits for its dom0 relay; physical boot is opt-in.
      wantedBy = lib.mkForce (lib.optional (!hybrid && cfg.physicalDesktopOnBoot) "graphical.target");
      requires = lib.optional hybrid "qubes-gui-agent.service";
      after = [ "airlock-qubes-rw-init.service" ] ++ lib.optional hybrid "qubes-gui-agent.service";
    };
    systemd.packages = [ guiAgent ];
    systemd.services.qubes-gui-agent = {
      enable = hybrid;
      wantedBy = [ "multi-user.target" ];
      after = [
        "qubes-db.service"
        "qubes-qrexec-agent.service"
        "systemd-user-sessions.service"
      ];
      # LightDM manages Xephyr, not the outer Xorg server: both must run.
      unitConfig.ConditionPathExists = lib.mkForce "";
      path = [
        pkgs.coreutils
        pkgs.gnused
        pkgs.qubes-core-agent-linux
        pkgs.qubes-core-qubesdb
        pkgs.shadow
        pkgs.util-linux
        pkgs.systemd
      ];
    };

    # Use NixOS PAM modules and session setup. The packaged Fedora PAM file
    # includes system-auth/postlogin, neither of which exists on NixOS.
    security.pam.services.qubes-gui-agent = {
      rootOK = true;
      startSession = true;
    };

    environment.etc = {
      "qubes-rpc/qubes.SyncAppMenus" = {
        source = receiveAppmenus;
        mode = "0555";
      };
      "qubes/rpc-config/qubes.SyncAppMenus" = {
        text = "force-user='${guestUser}'\n";
        mode = "0444";
      };
      "X11/xorg-qubes.conf.template".source = "${guiAgent}/etc/X11/xorg-qubes.conf.template";
      # Only profiles that explicitly enable XFCE should retain its session.
      "X11/xinit/xinitrc".source = if cfg.enableXfce then
        pkgs.writeShellScript "airlock-qubes-xfce-session" ''
          export XSESSION=${pkgs.xfce4-session}/bin/xfce4-session
          exec ${guiAgent}/bin/qubes-session
        ''
      else "${guiAgent}/bin/qubes-session";
      "qubes-rpc/qubes.GuiVMSession" = lib.mkIf hybrid { source = guiSession; };
      "qubes-rpc/qubes.SetMonitorLayout".source = "${guiAgent}/etc/qubes-rpc/qubes.SetMonitorLayout";
      "qubes-rpc/qubes.WindowIconUpdater".source =
        "${pkgs.qubes-gui-daemon}/etc/qubes-rpc/qubes.WindowIconUpdater";
      "qubes/guid.conf".source = "${pkgs.qubes-gui-daemon}/etc/qubes/guid.conf";
      "qubes/rpc-config/qubes.GuiVMSession".source =
        "${guiAgent}/etc/qubes/rpc-config/qubes.GuiVMSession";
      "qubes/rpc-config/qubes.SetMonitorLayout".text = "force-user='${guestUser}'\n";
      "qubes/rpc-config/qubes.WindowIconUpdater".source =
        "${pkgs.qubes-gui-daemon}/etc/qubes/rpc-config/qubes.WindowIconUpdater";
      "xdg/autostart/qubes-icon-receiver.desktop".source =
        "${pkgs.qubes-gui-daemon}/etc/xdg/autostart/qubes-icon-receiver.desktop";
      "xdg/autostart/qubes-screen-layout-watches.desktop".source =
        "${pkgs.qubes-gui-daemon}/etc/xdg/autostart/qubes-screen-layout-watches.desktop";
    };

    # Run in the desktop user's session, where GNOME monitors the normal XDG
    # application directory. Stopped VMs use the persistent upstream cache.
    systemd.user.services.airlock-application-menus = lib.mkIf cfg.applicationMenus.enable {
      description = "Refresh applications from VMs assigned to this GUI VM";
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${applicationMenus}/bin/airlock-application-menus --reconcile";
        TimeoutStartSec = "10min";
        KillMode = "control-group";
      };
    };
    systemd.user.timers.airlock-application-menus = lib.mkIf cfg.applicationMenus.enable {
      wantedBy = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      timerConfig = {
        OnActiveSec = "2s";
        OnUnitInactiveSec = "30s";
        AccuracySec = "1s";
      };
    };

    xdg.mime.defaultApplications = lib.mkIf (cfg.defaultApplicationVM != null) {
      "x-scheme-handler/http" = "airlock-open-url.desktop";
      "x-scheme-handler/https" = "airlock-open-url.desktop";
    };

    systemd.tmpfiles.rules = [
      "d /var/log/qubes 2775 root qubes - -"
      "f /run/qubes-service/guivm 0644 root root - -"
      "f /run/qubes-service-environment 0644 root root - -"
    ] ++ lib.optional hybrid "f /run/qubes-service/guivm-gui-agent 0644 root root - -";
  };
}
