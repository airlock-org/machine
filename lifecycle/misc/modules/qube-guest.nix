{
  config,
  lib,
  airlockConfigInputs ? null,
  airlockPrebuiltTemplateSources ? { },
  pkgs,
  ...
}:
let
  cfg = config.airlock.qubeGuest;
  hasConfigVaultRole = cfg.role == "sys-config-vault";
  hasConfigStagingRole = cfg.role == "sys-config-staging";
  hasConfigValidatorRole = cfg.role == "sys-config-validator";
  hasCmdRole = cfg.role == "cmd";
  hasSysNetRole = cfg.role == "sys-net";
  hasSysDevRole = cfg.role == "sys-dev";
  hasNetworkDriverRole = hasSysNetRole || hasSysDevRole;
  hasSysGuiRole = cfg.role == "sys-gui";
  hasNetworkProviderRole = hasSysNetRole || hasSysDevRole || cfg.role == "sys-firewall";
  hasConfigRole = hasConfigVaultRole || hasConfigStagingRole || hasConfigValidatorRole;
  hasPrivateNixRole =
    hasConfigVaultRole
    || hasConfigStagingRole
    || hasConfigValidatorRole
    || hasCmdRole
    || hasSysDevRole;
  hasConfigInputsImportRole = hasConfigStagingRole || hasConfigValidatorRole;
  qubesAgentLinux = pkgs.qubes-core-agent-linux;
  qubesDbPython =
    (pkgs.qubes-core-qubesdb.override {
      withPython = true;
      python = pkgs.python3;
    }).pythonModule;
  qubesPython = pkgs.python3.withPackages (ps: [
    ps.dbus-python
    ps.pygobject3
    ps.pyxdg
    qubesDbPython
  ]);
  # The packaged agent installs this upstream qrexec handler as source only.
  # Run it with an interpreter that includes its Python dependencies and make
  # the agent's private Python modules available without relying on /usr paths.
  qubesStartAppService = pkgs.writeShellScript "airlock-qubes-start-app" ''
    if [ -r /etc/profile ]; then
      . /etc/profile
    fi
    export XDG_DATA_DIRS="/run/current-system/sw/share:''${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
    export PYTHONPATH="${qubesAgentLinux}/${pkgs.python3.sitePackages}''${PYTHONPATH:+:$PYTHONPATH}"
    exec ${qubesPython}/bin/python ${qubesAgentLinux.src}/qubes-rpc/qubes.StartApp "$@"
  '';
  roleGit = pkgs.gitMinimal;
  airlockCli = pkgs.callPackage ../../runtime/tools/pkgs/airlock-cli { };
  poolArtifacts = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
  systemdPackage = config.systemd.package;
  configInputsPublicPath = "/etc/airlock/config-inputs";
  cmdPersistentPaths = lib.unique (cfg.cmdPersistentPaths ++ [ cfg.codexHome ]);

  qubesGuestPath = [
    pkgs.coreutils
    pkgs.diffutils
    pkgs.e2fsprogs
    pkgs.findutils
    pkgs.gawk
    pkgs.glibc.bin
    pkgs.gnugrep
    pkgs.kmod
    pkgs.qubes-core-qubesdb
    pkgs.qubes-linux-utils
    systemdPackage
    pkgs.util-linux
  ];

  guestDiagnosticsPath = [
    pkgs.coreutils
    pkgs.findutils
    pkgs.gnugrep
    pkgs.kmod
    pkgs.qubes-core-qubesdb
    systemdPackage
    pkgs.util-linux
  ];

  networkProviderPackages = [
    pkgs.conntrack-tools
    pkgs.iproute2
    pkgs.iptables
    pkgs.networkmanager
    pkgs.nftables
    pkgs.procps
    pkgs.qubes-vmm-xen
    pkgs.tinyproxy
  ];

  codexAuthImportService = pkgs.writeShellScript "airlock-codex-auth-import" ''
    set -euo pipefail

    codex_home=${lib.escapeShellArg cfg.codexHome}
    temp="$(mktemp "''${TMPDIR:-/tmp}/airlock-codex-auth.XXXXXX")"
    trap 'rm -f "$temp"' EXIT
    ${pkgs.coreutils}/bin/cat > "$temp"
    ${pkgs.jq}/bin/jq -e type "$temp" >/dev/null
    ${pkgs.coreutils}/bin/install -d -m 0700 "$codex_home"
    ${pkgs.coreutils}/bin/install -m 0600 "$temp" "$codex_home/auth.json"
  '';
  codexConfigImportService = pkgs.writeShellScript "airlock-codex-config-import" ''
    set -euo pipefail

    codex_home=${lib.escapeShellArg cfg.codexHome}
    temp="$(mktemp "''${TMPDIR:-/tmp}/airlock-codex-config.XXXXXX")"
    trap 'rm -f "$temp"' EXIT
    ${pkgs.coreutils}/bin/cat > "$temp"
    ${pkgs.coreutils}/bin/install -d -m 0700 "$codex_home"
    ${pkgs.coreutils}/bin/install -m 0600 "$temp" "$codex_home/config.toml"
  '';

  roleInitPathFor =
    role:
    lib.makeBinPath (
      [
        pkgs.coreutils
        pkgs.gawk
        roleGit
        pkgs.gnutar
        pkgs.jq
        pkgs.qubes-core-qubesdb
        pkgs.util-linux
      ]
      ++
        lib.optionals
          (
            role == "sys-config-vault"
            || role == "sys-config-staging"
            || role == "sys-config-validator"
            || role == "cmd"
          )
          [
            configNix
            pkgs.nix
          ]
    );

  rolePackages = {
    base = [ ];
    sys-net = networkProviderPackages;
    sys-firewall = networkProviderPackages;
    sys-dev = [
      (lib.hiPrio configNix)
      poolSend
    ];
    sys-config-vault = [
      configVaultInit
      configVaultArchiveCandidate
      configVaultArchiveDeployment
      configVaultExportCurrent
      (lib.hiPrio configNix)
      pkgs.gawk
      roleGit
      pkgs.gnutar
      pkgs.jq
    ];
    sys-config-staging = [
      configStagingExportCandidate
      configStagingImportCurrent
      configStagingInit
      configStagingValidate
      (lib.hiPrio configNix)
      pkgs.gawk
      roleGit
      pkgs.gnutar
      pkgs.jq
    ];
    sys-config-validator = [
      configValidatorValidateCandidate
      configValidatorInit
      (lib.hiPrio configNix)
      pkgs.gawk
      roleGit
      pkgs.gnutar
      pkgs.jq
    ];
    cmd = [
      (lib.hiPrio configNix)
      airlockCli
      pkgs.bashInteractive
      pkgs.cacert
      pkgs.cargo
      pkgs.clippy
      pkgs.codex
      pkgs.diffutils
      pkgs.fd
      pkgs.file
      pkgs.findutils
      pkgs.gawk
      pkgs.git
      pkgs.gnugrep
      pkgs.gnumake
      pkgs.gnused
      pkgs.gnutar
      pkgs.gzip
      pkgs.jq
      pkgs.less
      pkgs.openssh
      pkgs.procps
      pkgs.python3
      pkgs.ripgrep
      pkgs.rustc
      pkgs.uv
      pkgs.vim
      pkgs.which
      pkgs.xz
      pkgs.zellij
    ];
  };

  inherit (import ./qube-guest/config-bootstrap.nix {
    inherit
      cfg
      lib
      pkgs
      airlockPrebuiltTemplateSources
      ;
  })
    initialRuntimeConfigRepoGitAddArgs
    initialRuntimeConfigRepoFilesScript
    configInputPathSubstitutionScript
    ;

  configGuestInitCommand =
    if hasConfigVaultRole then
      "${configVaultInit}/bin/airlock-config-vault-init"
    else if hasConfigStagingRole then
      "${configStagingInit}/bin/airlock-config-staging-init"
    else if hasConfigValidatorRole then
      "${configValidatorInit}/bin/airlock-config-validator-init"
    else
      "${pkgs.coreutils}/bin/true";
  inherit (import ./qube-guest/private-nix.nix {
    inherit
      cfg
      lib
      pkgs
      ;
  })
    configNix
    ;

  poolSend = pkgs.writeShellScriptBin "airlock-pool-send" ''
    exec ${poolArtifacts}/bin/airlock-pool-send \
      --physical-store-root ${lib.escapeShellArg "${cfg.configNixStoreRoot}/nix/store"} \
      --nix ${configNix}/bin/airlock-config-nix \
      ${lib.optionalString (cfg.role != "sys-dev") "--nix-store ${lib.escapeShellArg cfg.configNixStoreRoot}"} \
      "$@"
  '';

  inherit (import ./qube-guest/config-vault.nix {
    inherit
      cfg
      pkgs
      roleGit
      configNix
      configInputsPublicPath
      configInputPathSubstitutionScript
      initialRuntimeConfigRepoFilesScript
      initialRuntimeConfigRepoGitAddArgs
      ;
  })
    configVaultInit
    configVaultExportCurrent
    configCurrentExportService
    configVaultArchiveCandidate
    configVaultArchiveDeployment
    configArchiveService
    configDeploymentArchiveService
    ;

  inherit (import ./qube-guest/config-staging.nix {
    inherit
      cfg
      lib
      pkgs
      roleGit
      configNix
      configInputsPublicPath
      configInputPathSubstitutionScript
      initialRuntimeConfigRepoFilesScript
      initialRuntimeConfigRepoGitAddArgs
      configValidatorValidateCandidate
      ;
  })
    configStagingImportCurrent
    configCurrentImportService
    configStagingInit
    configStagingValidate
    configStagingExportCandidate
    configCandidateExportService
    ;

  inherit (import ./qube-guest/config-validator.nix {
    inherit
      cfg
      pkgs
      roleGit
      configNix
      ;
  })
    configValidatorInit
    configValidatorValidateCandidate
    configCandidateValidateService
    ;

  inherit (import ./qube-guest/config-inputs.nix {
    inherit
      cfg
      lib
      pkgs
      configInputsPublicPath
      configStagingInit
      hasConfigStagingRole
      roleInitPathFor
      ;
  })
    configInputsImportService
    ;

  nixStoreRuntimeCheckService = pkgs.writeShellScript "airlock-nix-store-runtime-check-qrexec" ''
    set -eu

    export PATH=${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.gnugrep
        pkgs.gnused
        pkgs.util-linux
      ]
    }

    system_path="$(
      tr ' ' '\n' < /proc/cmdline |
        sed -n 's|^airlock.system=||p' |
        head -n 1
    )"
    store_device="$(
      tr ' ' '\n' < /proc/cmdline |
        sed -n 's|^airlock.storedev=||p' |
        head -n 1
    )"
    store_format="$(
      tr ' ' '\n' < /proc/cmdline |
        sed -n 's|^airlock.storefmt=||p' |
        head -n 1
    )"
    store_blob_device="$(
      tr ' ' '\n' < /proc/cmdline |
        sed -n 's|^airlock.storeblobdev=||p' |
        head -n 1
    )"
    [ -n "$store_format" ] || store_format="erofs"

    [ -n "$system_path" ] || {
      echo "missing airlock.system kernel option" >&2
      exit 1
    }
    [ -n "$store_device" ] || {
      echo "missing airlock.storedev kernel option" >&2
      exit 1
    }
    [ -x "$system_path/init" ] || {
      echo "system init is not executable: $system_path/init" >&2
      exit 1
    }

    current_system="$(readlink -f /run/current-system)"
    [ "$current_system" = "$system_path" ] || {
      echo "current system mismatch: $current_system != $system_path" >&2
      exit 1
    }

    mount_source="$(findmnt -rn -T /nix/store -o SOURCE)"
    mount_options="$(findmnt -rn -T /nix/store -o OPTIONS)"

    [ "$mount_source" = "$store_device" ] || {
      echo "/nix/store source mismatch: $mount_source != $store_device" >&2
      exit 1
    }
    if [ "$store_format" = "erofs" ] && [ -n "$store_blob_device" ]; then
      [ -b "$store_blob_device" ] || {
        echo "store blob device is missing: $store_blob_device" >&2
        exit 1
      }
    fi
    case ",$mount_options," in
      *,ro,*) ;;
      *)
        echo "/nix/store is not mounted read-only: $mount_options" >&2
        exit 1
        ;;
    esac

    write_probe="/nix/store/.airlock-runtime-write-probe-$$"
    if touch "$write_probe" 2>/tmp/airlock-runtime-write-probe.err; then
      rm -f "$write_probe"
      echo "/nix/store accepted a write probe" >&2
      exit 1
    fi

    printf 'system=%s\n' "$system_path"
    printf 'store_device=%s\n' "$store_device"
    printf 'store_format=%s\n' "$store_format"
    printf 'store_blob_device=%s\n' "$store_blob_device"
    printf 'store_options=%s\n' "$mount_options"
  '';

  policyProbeService = pkgs.writeShellScript "airlock-policy-probe-qrexec" ''
    set -eu

    role=${lib.escapeShellArg cfg.role}
    qubesdb_read="''${AIRLOCK_QUBESDB_READ:-${pkgs.qubes-core-qubesdb}/bin/qubesdb-read}"
    qdb_role="$("$qubesdb_read" /features/airlock-role 2>/dev/null || true)"
    vm_name="$("$qubesdb_read" /name 2>/dev/null || true)"

    case "$qdb_role" in
      sys-config-vault|sys-config-staging|sys-config-validator) role="$qdb_role" ;;
      "") ;;
      *) role=base ;;
    esac

    case "$vm_name" in
      sys-config-vault) role=sys-config-vault ;;
      sys-config-staging) role=sys-config-staging ;;
      sys-config-validator) role=sys-config-validator ;;
    esac

    printf 'airlock-policy-probe role=%s user=%s\n' "$role" "$(${pkgs.coreutils}/bin/whoami)"
  '';

  vmShellService = pkgs.writeShellScript "airlock-qubes-vmshell" ''
    export PATH=/run/current-system/sw/bin:/run/wrappers/bin:/bin:/usr/bin
    exec ${pkgs.runtimeShell}
  '';

  getAppmenusService = pkgs.writeShellScript "airlock-qubes-get-appmenus" ''
    # Keep this close to upstream qubes.GetAppmenus, but use NixOS paths.
    if [ -r /etc/profile ]; then
      # shellcheck disable=SC1091
      . /etc/profile
    fi

    export PATH=${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.findutils
        pkgs.gawk
        pkgs.glibc.bin
        pkgs.qubes-core-qubesdb
      ]
    }:$PATH

    if [ -z "''${XDG_DATA_HOME:-}" ]; then
      qrexec_user="$(whoami)"
      if [ "$qrexec_user" = root ]; then
        qrexec_user="$(qubesdb-read /default-user 2>/dev/null || printf '%s\n' ${lib.escapeShellArg cfg.user})"
      fi

      home="$(getent passwd "$qrexec_user" | cut -d: -f6)"
      if [ -z "$home" ]; then
        home="/home/$qrexec_user"
      fi
      XDG_DATA_HOME="$home/.local/share"
    fi

    if [ -z "''${XDG_DATA_DIRS:-}" ]; then
      XDG_DATA_DIRS="/run/current-system/sw/share:/usr/local/share:/usr/share"
    fi

    persistence="$(qubesdb-read /qubes-vm-persistence 2>/dev/null || printf 'full\n')"
    rw_devno=""
    if [ -e /rw ]; then
      rw_devno="$(stat -c %D /rw 2>/dev/null || true)"
    fi

    apps_dirs_to_consider=( "$XDG_DATA_HOME" )
    old_IFS="$IFS"
    IFS=:
    # shellcheck disable=SC2206
    apps_dirs_to_consider+=( $XDG_DATA_DIRS )
    IFS="$old_IFS"

    apps_dirs=()
    for dir in "''${apps_dirs_to_consider[@]}"; do
      case "$dir" in
        /*) ;;
        *) continue ;;
      esac

      if [ "$persistence" = full ]; then
        apps_dirs+=( "$dir/applications" )
      elif [ "$persistence" = rw-only ] &&
        [ -n "$rw_devno" ] &&
        [ -e "$dir" ] &&
        [ "$(stat -c %D "$dir" 2>/dev/null || true)" = "$rw_devno" ]; then
        apps_dirs+=( "$dir/applications" )
      fi
    done

    for app_dir in "''${apps_dirs[@]}"; do
      case "$app_dir" in
        /*) ;;
        *) continue ;;
      esac

      [ -d "$app_dir" ] || continue

      (
        cd "$app_dir"
        find . -name '*.desktop' -exec awk -b '
          BEGINFILE { if (ERRNO) nextfile; entry=""; file = substr(FILENAME, 3); gsub(/\//, "-", file) }
          /^\[/ { if (tolower($0) != "\[desktop entry\]") nextfile }
          /^Exec *=/ { entry = entry file ":Exec=qubes-desktop-run " file "\n"; next }
          /^NoDisplay *= *true$/ { entry=""; nextfile }
          /^Categories *= *Screensaver;$/ { entry=""; nextfile }
          /=/ { entry = entry file ":" $0 "\n" }
          ENDFILE { print entry }
        ' 2>/dev/null '{}' +
      )
    done
  '';

in
{
  imports = [
    (import ./qube-guest/network.nix {
      inherit config lib pkgs qubesGuestPath qubesPython networkProviderPackages;
    })
    ./sys-gui/base.nix
  ];

  options.airlock.qubeGuest = {
    enable = lib.mkEnableOption "a Airlock NixOS Qubes guest domain";

    role = lib.mkOption {
      type = lib.types.enum [
        "base"
        "sys-config-vault"
        "sys-config-staging"
        "sys-config-validator"
        "cmd"
        "sys-firewall"
        "sys-gui"
        "sys-net"
        "sys-dev"
      ];
      default = "base";
      description = "Airlock role-specific userspace to place in the guest domain.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "user";
      description = "Primary unprivileged user in the guest domain.";
    };

    allowQrexecUserSelection = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Allow callers to select the target user for qubes.VMShell qrexec requests.";
    };

    configRepoPath = lib.mkOption {
      type = lib.types.str;
      default = "/home/${cfg.user}/airlock-config";
      description = "Editable config repository path in the sys-config-staging role.";
    };

    configValidationPath = lib.mkOption {
      type = lib.types.str;
      default = "/home/${cfg.user}/airlock-config-validation";
      description = "Directory for sys-config-staging and sys-config-validator validation results.";
    };

    configArchivePath = lib.mkOption {
      type = lib.types.str;
      default = "/home/${cfg.user}/airlock-config-vault";
      description = "Archived candidate repository path in the sys-config-vault role.";
    };

    configNixStoreRoot = lib.mkOption {
      type = lib.types.str;
      default = "/rw/home/${cfg.user}/airlock-config-nix-root";
      description = "Private writable chroot Nix store root for config validation in read-only-store guests.";
    };

    configInputsPath = lib.mkOption {
      type = lib.types.str;
      default = "/rw/home/${cfg.user}/airlock-config-inputs";
      description = "Persistent path where dom0 imports offline config flake config inputs.";
    };

    codexHome = lib.mkOption {
      type = lib.types.str;
      default = "/home/${cfg.user}/.codex";
      description = "Persistent Codex CLI state directory. This maps to CODEX_HOME.";
    };

    cmdPersistentPaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "/home/${cfg.user}/dev" ];
      description = "Persistent user paths initialized by the command-workspace role.";
    };
  };

  config = lib.mkIf cfg.enable {
    airlock.sysGuiGuest.enable = hasSysGuiRole;
    assertions = [
      {
        assertion = cfg.user != "";
        message = "airlock.qubeGuest.user must not be empty.";
      }
      {
        assertion = lib.hasPrefix "/home/" cfg.configRepoPath;
        message = "airlock.qubeGuest.configRepoPath should live under /home for Qubes private storage.";
      }
      {
        assertion = lib.hasPrefix "/home/" cfg.configValidationPath;
        message = "airlock.qubeGuest.configValidationPath should live under /home for Qubes private storage.";
      }
      {
        assertion = lib.hasPrefix "/home/" cfg.configArchivePath;
        message = "airlock.qubeGuest.configArchivePath should live under /home for Qubes private storage.";
      }
      {
        assertion = lib.hasPrefix "/rw/" cfg.configNixStoreRoot;
        message = "airlock.qubeGuest.configNixStoreRoot should live under /rw for Qubes private storage.";
      }
      {
        assertion = lib.hasPrefix "/rw/" cfg.configInputsPath;
        message = "airlock.qubeGuest.configInputsPath should live under /rw for Qubes private storage.";
      }
      {
        assertion = lib.hasPrefix "/home/" cfg.codexHome;
        message = "airlock.qubeGuest.codexHome should live under /home for Qubes private storage.";
      }
      {
        assertion = lib.all (path: lib.hasPrefix "/home/" path) cmdPersistentPaths;
        message = "airlock.qubeGuest.cmdPersistentPaths entries should live under /home for Qubes private storage.";
      }
    ];

    boot.loader.grub.enable = false;
    boot.initrd.systemd.enable = lib.mkDefault true;
    airlock.minimalGuest.nix.enable = lib.mkDefault hasPrivateNixRole;
    boot.initrd.availableKernelModules = [
      "xen-blkfront"
      "xen-fbfront"
      "xen-kbdfront"
      "xen-netfront"
      "xen-pcifront"
      "xen-scsifront"
      "xen-tpmfront"
    ];
    boot.initrd.kernelModules = [
      "xen-blkfront"
      "xen-netfront"
      "dm_mod"
      "dm_snapshot"
    ];
    boot.kernelModules = [
      "xen-evtchn"
      "xen-gntalloc"
      "xen-gntdev"
      "xen-privcmd"
    ]
    ++ lib.optional hasNetworkProviderRole "xen-netback";
    boot.kernelParams = [
      "console=hvc0"
    ];

    fileSystems."/" = {
      device = "/dev/mapper/dmroot";
      fsType = "ext4";
    };

    networking.hostName = lib.mkDefault "airlock-qube";
    users.mutableUsers = lib.mkDefault false;
    users.allowNoPasswordLogin = lib.mkDefault true;
    users.groups.qubes = { };
    users.users.${cfg.user} = {
      isNormalUser = true;
      group = "users";
      extraGroups = [ "qubes" ];
    };
    security.pam.services.qrexec = {
      rootOK = true;
      startSession = true;
      setLoginUid = false;
    };

    systemd.tmpfiles.packages = [
      qubesAgentLinux
      pkgs.qubes-linux-utils
    ];

    systemd.tmpfiles.settings."10-airlock-qubes-guest" = {
      "/run/qubes".d = {
        user = "root";
        group = "qubes";
        mode = "0770";
      };
      "/var/log/qubes".d = {
        user = "root";
        group = "qubes";
        mode = "0770";
      };
      "/var/lib/qubes".d = {
        user = "root";
        group = "root";
        mode = "0755";
      };
    };

    systemd.packages = [
      pkgs.qubes-core-qubesdb.daemon
      pkgs.python3.pkgs.qubes-core-qrexec.domU
      pkgs.qubes-linux-utils
    ]
    ++ lib.optional hasNetworkProviderRole pkgs.qubes-vmm-xen;

    systemd.services.qubes-sysinit = {
      enable = true;
      wantedBy = [ "sysinit.target" ];
      after = [
        "systemd-modules-load.service"
        "qubes-db.service"
      ];
      requires = [ "qubes-db.service" ];
      before = [ "sysinit.target" ];
      path = qubesGuestPath;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${qubesAgentLinux}/lib/qubes/init/qubes-sysinit.sh";
      };
      unitConfig.DefaultDependencies = false;
    };

    systemd.services.airlock-qubes-db-diagnostics = {
      description = "Print QubesDB guest diagnostics to the console";
      path = guestDiagnosticsPath;
      script = ''
        set +e
        echo "airlock guest diagnostics: qubes-db.service failed"
        systemctl --no-pager status qubes-db.service
        journalctl -b --no-pager -u qubes-db.service -n 120
        echo "airlock guest diagnostics: Xen device nodes"
        ls -l /dev/xen /proc/xen
        echo "airlock guest diagnostics: loaded Xen modules"
        lsmod | grep -E '(^xen|evtchn|gnt|privcmd)'
      '';
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    systemd.services.airlock-qubes-qrexec-diagnostics = {
      description = "Print qrexec guest diagnostics to the console";
      path = guestDiagnosticsPath;
      script = ''
        set +e
        echo "airlock guest diagnostics: qubes-qrexec-agent.service failed"
        systemctl --no-pager status qubes-qrexec-agent.service
        journalctl -b --no-pager -u qubes-qrexec-agent.service -n 120
        echo "airlock guest diagnostics: QubesDB service state"
        systemctl --no-pager status qubes-db.service
      '';
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    systemd.services.airlock-qubes-rw-init-diagnostics = {
      description = "Print Qubes private storage diagnostics to the console";
      path = guestDiagnosticsPath;
      script = ''
        set +e
        echo "airlock guest diagnostics: airlock-qubes-rw-init.service failed"
        systemctl --no-pager status airlock-qubes-rw-init.service
        journalctl -b --no-pager -u airlock-qubes-rw-init.service -n 120
        echo "airlock guest diagnostics: filesystem state"
        ls -ld /rw /home /usr/local /var /var/run /run/qubes /var/lib /var/lib/qubes
        findmnt / /rw /home /usr/local
        echo "airlock guest diagnostics: block devices"
        lsblk -o NAME,MAJ:MIN,SIZE,FSTYPE,MOUNTPOINTS
        blkid /dev/xvdb
      '';
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    systemd.services.airlock-qubes-rw-init = {
      description = "Initialize Qubes private storage mounts for NixOS guests";
      wantedBy = [ "multi-user.target" ];
      wants = [ "dev-xvdb.device" ];
      after = [
        "dev-xvdb.device"
        "qubes-sysinit.service"
      ];
      requires = [ "qubes-sysinit.service" ];
      before = [
        "systemd-user-sessions.service"
        "qubes-qrexec-agent.service"
      ];
      path = qubesGuestPath;
      script = ''
        set -eu

        mkdir -p /rw /home /usr/local
        mkdir -p /var/lib/qubes

        if [ ! -e /dev/xvdb ]; then
          echo "Qubes private device /dev/xvdb is not present" >&2
          exit 1
        fi

        ${qubesAgentLinux}/lib/qubes/init/setup-rwdev.sh

        if ! mountpoint -q /rw; then
          mount -t auto -o defaults,discard,nosuid,nodev /dev/xvdb /rw
        fi

        ${qubesAgentLinux}/lib/qubes/init/setup-rw.sh

        . ${qubesAgentLinux}/lib/qubes/init/functions
        initialize_home /rw/home ifneeded

        if ! mountpoint -q /home; then
          mount --bind /rw/home /home
          mount -o remount,bind,nosuid,nodev /home
        fi

        install -d -m 0700 -o ${cfg.user} -g users /home/${cfg.user}

        mkdir -p /rw/usrlocal
        if ! mountpoint -q /usr/local; then
          mount --bind /rw/usrlocal /usr/local
        fi
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      unitConfig.OnFailure = [ "airlock-qubes-rw-init-diagnostics.service" ];
    };

    systemd.services.airlock-config-inputs-mount = lib.mkIf hasConfigInputsImportRole {
      description = "Expose imported Airlock config inputs";
      wantedBy = [ "multi-user.target" ];
      wants = [ "airlock-qubes-rw-init.service" ];
      requires = [ "airlock-qubes-rw-init.service" ];
      after = [ "airlock-qubes-rw-init.service" ];
      before = [
        "airlock-config-guest-init.service"
        "qubes-qrexec-agent.service"
      ];
      path = [
        pkgs.coreutils
        pkgs.gnused
        pkgs.jq
        pkgs.util-linux
      ];
      script = ''
        set -eu

        target_root=${lib.escapeShellArg cfg.configInputsPath}
        store_root=${lib.escapeShellArg cfg.configNixStoreRoot}
        public_root=${lib.escapeShellArg configInputsPublicPath}
        manifest="$target_root/manifest.json"

        mkdir -p "$target_root" "$public_root"
        [ -f "$manifest" ] || exit 0

        jq -r '.inputs | to_entries[] | [.key, .value] | @tsv' "$manifest" |
          while IFS="$(printf '\t')" read -r name input_path; do
            case "$name" in
              nixpkgs|nixpkgs-libvirt_10_5|nixos-qubes|airlock-support) ;;
              *)
                printf 'invalid config input name: %s\n' "$name" >&2
                exit 1
                ;;
            esac
            input_store_path="$(printf '%s\n' "$input_path" | sed 's|^\(/nix/store/[^/]*\).*|\1|')"
            input_relative_path="''${input_path#"$input_store_path"}"
            physical_path="$store_root$input_store_path$input_relative_path"
            [ -d "$physical_path" ] || {
              printf 'imported config input path is missing from private Nix store: %s\n' "$physical_path" >&2
              exit 1
            }
            mountpoint_path="$public_root/$name"
            if mountpoint -q "$mountpoint_path"; then
              continue
            fi
            if [ -e "$mountpoint_path" ] && [ ! -d "$mountpoint_path" ]; then
              rm -rf "$mountpoint_path"
            fi
            mkdir -p "$mountpoint_path"
            mount --bind "$physical_path" "$mountpoint_path"
          done
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
    };

    systemd.services.airlock-config-guest-init-diagnostics = lib.mkIf hasConfigRole {
      description = "Print Airlock config guest diagnostics to the console";
      path = guestDiagnosticsPath;
      script = ''
        set +e
        echo "airlock guest diagnostics: airlock-config-guest-init.service failed"
        systemctl --no-pager status airlock-config-guest-init.service
        journalctl -b --no-pager -u airlock-config-guest-init.service -n 160
        echo "airlock guest diagnostics: Qubes identity"
        qubesdb-read /name
        qubesdb-read /features/airlock-role
        echo "airlock guest diagnostics: filesystem state"
        findmnt / /rw /home /nix /nix/store
        ls -ld /rw /rw/home /home /home/${cfg.user} /nix /nix/store
        echo "airlock guest diagnostics: config paths"
        find /home/${cfg.user} -maxdepth 3 -mindepth 1 -print
      '';
      serviceConfig = {
        Type = "oneshot";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    systemd.services.airlock-config-guest-init = lib.mkIf hasConfigRole {
      description = "Initialize Airlock config repositories in Qubes private storage";
      wantedBy = [ "multi-user.target" ];
      wants = [
        "airlock-qubes-rw-init.service"
      ]
      ++ lib.optionals hasConfigInputsImportRole [ "airlock-config-inputs-mount.service" ];
      requires = [
        "airlock-qubes-rw-init.service"
      ]
      ++ lib.optionals hasConfigInputsImportRole [ "airlock-config-inputs-mount.service" ];
      after = [
        "airlock-qubes-rw-init.service"
        "qubes-db.service"
      ]
      ++ lib.optionals hasConfigInputsImportRole [ "airlock-config-inputs-mount.service" ];
      before = [ "qubes-qrexec-agent.service" ];
      path = [
        pkgs.coreutils
        pkgs.qubes-core-qubesdb
        pkgs.util-linux
      ];
      script = ''
        set -eu

        expected_role=${lib.escapeShellArg cfg.role}
        vm_name="$(qubesdb-read /name 2>/dev/null || true)"
        qdb_role="$(qubesdb-read /features/airlock-role 2>/dev/null || true)"

        case "$qdb_role" in
          "") ;;
          "$expected_role") ;;
          sys-config-vault|sys-config-staging|sys-config-validator)
            echo "airlock config guest init: QubesDB role mismatch: expected=$expected_role actual=$qdb_role vm=$vm_name" >&2
            exit 1
            ;;
          *)
            echo "airlock config guest init: unsupported QubesDB role: expected=$expected_role actual=$qdb_role vm=$vm_name" >&2
            exit 1
            ;;
        esac

        case "$vm_name" in
          ""|"$expected_role") ;;
          sys-config-vault|sys-config-staging|sys-config-validator)
            echo "airlock config guest init: VM name mismatch: expected=$expected_role actual=$vm_name qdb_role=$qdb_role" >&2
            exit 1
            ;;
          *) ;;
        esac

        run_as_user() {
          ${pkgs.util-linux}/bin/runuser -u ${lib.escapeShellArg cfg.user} -- \
            ${pkgs.coreutils}/bin/env \
              HOME=/home/${lib.escapeShellArg cfg.user} \
              USER=${lib.escapeShellArg cfg.user} \
              LOGNAME=${lib.escapeShellArg cfg.user} \
              XDG_CACHE_HOME=/home/${lib.escapeShellArg cfg.user}/.cache \
              PATH=${lib.escapeShellArg (roleInitPathFor cfg.role)} \
              "$@"
        }

        run_as_user ${configGuestInitCommand}
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      unitConfig.OnFailure = [ "airlock-config-guest-init-diagnostics.service" ];
    };

    systemd.services.airlock-cmd-guest-init = lib.mkIf hasCmdRole {
      description = "Initialize persistent Airlock command-workspace state";
      wantedBy = [ "multi-user.target" ];
      wants = [ "airlock-qubes-rw-init.service" ];
      requires = [ "airlock-qubes-rw-init.service" ];
      after = [ "airlock-qubes-rw-init.service" ];
      before = [ "qubes-qrexec-agent.service" ];
      path = [
        pkgs.coreutils
        pkgs.util-linux
      ];
      script =
        let
          installPersistentPath =
            path:
            let
              mode = if path == cfg.codexHome then "0700" else "0755";
            in
            ''
              install -d -m ${mode} -o ${lib.escapeShellArg cfg.user} -g users ${lib.escapeShellArg path}
            '';
        in
        ''
          set -eu

          install -d -m 0700 -o ${lib.escapeShellArg cfg.user} -g users ${lib.escapeShellArg cfg.configNixStoreRoot}
        ''
        + lib.concatMapStrings installPersistentPath cmdPersistentPaths;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
    };

    systemd.services.qubes-db = {
      enable = true;
      wantedBy = [ "sysinit.target" ];
      after = [
        "systemd-modules-load.service"
        "systemd-tmpfiles-setup.service"
      ];
      requires = [ "systemd-tmpfiles-setup.service" ];
      overrideStrategy = "asDropin";
      unitConfig.OnFailure = [ "airlock-qubes-db-diagnostics.service" ];
    };

    systemd.services.qubes-qrexec-agent =
      let
        roleInitDeps =
          lib.optional hasConfigRole "airlock-config-guest-init.service"
          ++ lib.optional hasCmdRole "airlock-cmd-guest-init.service";
        normalAfter = [
          "airlock-qubes-rw-init.service"
          "systemd-user-sessions.service"
        ]
        ++ roleInitDeps;
        networkProviderAfter = [
          "xendriverdomain.service"
          "airlock-qubes-rw-init.service"
        ]
        ++ roleInitDeps;
      in
      {
        enable = true;
        wantedBy = [ "multi-user.target" ];
        wants = [ "airlock-qubes-rw-init.service" ];
        requires = [
          "airlock-qubes-rw-init.service"
        ]
        ++ roleInitDeps;
        after = if hasNetworkProviderRole then lib.mkForce networkProviderAfter else normalAfter;
        before = lib.optionals hasNetworkProviderRole [ "network.target" ];
        aliases = [ "qubes-core-agent.service" ];
        overrideStrategy = "asDropin";
        unitConfig = {
          OnFailure = [ "airlock-qubes-qrexec-diagnostics.service" ];
        };
      };

    systemd.services.qubes-meminfo-writer = {
      enable = true;
      wantedBy = [ "multi-user.target" ];
      before = [ "systemd-user-sessions.service" ];
    };

    environment.etc."qubes-rpc/airlock.Config.CandidateExport" = lib.mkIf hasConfigStagingRole {
      source = configCandidateExportService;
      mode = "0555";
    };

    environment.etc."qubes-rpc/airlock.Config.ImportCurrent" = lib.mkIf hasConfigStagingRole {
      source = configCurrentImportService;
      mode = "0555";
    };

    environment.etc."airlock/guest-tools/airlock-config-staging-validate" =
      lib.mkIf hasConfigStagingRole
        {
          source = configStagingValidate;
        };

    environment.etc."qubes/rpc-config/airlock.Config.CandidateExport" = lib.mkIf hasConfigStagingRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes/rpc-config/airlock.Config.ImportCurrent" = lib.mkIf hasConfigStagingRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.Config.ValidateCandidate" = lib.mkIf hasConfigValidatorRole {
      source = configCandidateValidateService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.Config.ValidateCandidate" =
      lib.mkIf hasConfigValidatorRole
        {
          text = "force-user='${cfg.user}'\n";
          mode = "0444";
        };

    environment.etc."qubes-rpc/airlock.Config.InputsImport" = lib.mkIf hasConfigInputsImportRole {
      source = configInputsImportService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.Config.InputsImport" = lib.mkIf hasConfigInputsImportRole {
      text = "force-user='root'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.Config.ArchiveCandidate" = lib.mkIf hasConfigVaultRole {
      source = configArchiveService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.Config.ArchiveCandidate" = lib.mkIf hasConfigVaultRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.Config.ArchiveDeployment" = lib.mkIf hasConfigVaultRole {
      source = configDeploymentArchiveService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.Config.ArchiveDeployment" = lib.mkIf hasConfigVaultRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.Config.ExportCurrent" = lib.mkIf hasConfigVaultRole {
      source = configCurrentExportService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.Config.ExportCurrent" = lib.mkIf hasConfigVaultRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.NixStoreRuntimeCheck" = {
      source = nixStoreRuntimeCheckService;
      mode = "0555";
    };

    environment.etc."qubes-rpc/airlock.PolicyProbe" = lib.mkIf hasConfigRole {
      source = policyProbeService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.PolicyProbe" = lib.mkIf hasConfigRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/qubes.VMShell" = {
      source = vmShellService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/qubes.VMShell" = {
      text = lib.optionalString (!cfg.allowQrexecUserSelection) "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/qubes.StartApp" = {
      source = qubesStartAppService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/qubes.StartApp" = {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/qubes.GetAppmenus" = {
      source = getAppmenusService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/qubes.GetAppmenus" = {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.CodexAuthImport" = lib.mkIf hasCmdRole {
      source = codexAuthImportService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.CodexAuthImport" = lib.mkIf hasCmdRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.etc."qubes-rpc/airlock.CodexConfigImport" = lib.mkIf hasCmdRole {
      source = codexConfigImportService;
      mode = "0555";
    };

    environment.etc."qubes/rpc-config/airlock.CodexConfigImport" = lib.mkIf hasCmdRole {
      text = "force-user='${cfg.user}'\n";
      mode = "0444";
    };

    environment.systemPackages = [
      pkgs.qubes-core-qubesdb
      qubesAgentLinux
      pkgs.qubes-linux-utils
      pkgs.python3.pkgs.qubes-core-qrexec
      pkgs.python3.pkgs.qubes-core-qrexec.domU
    ]
    ++ (rolePackages.${cfg.role} or [ ]);

    environment.sessionVariables = lib.mkIf hasCmdRole {
      CODEX_HOME = cfg.codexHome;
    };

    nix.settings = lib.mkIf hasPrivateNixRole {
      allowed-users = [ cfg.user ];
      experimental-features = [
        "nix-command"
        "flakes"
      ];
    };

    services.timesyncd.enable = false;
    system.stateVersion = "26.05";
  };
}
