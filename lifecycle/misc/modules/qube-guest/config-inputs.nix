{
  cfg,
  lib,
  pkgs,
  configInputsPublicPath,
  configStagingInit,
  hasConfigStagingRole,
  roleInitPathFor,
}:
rec {
  configInputsImportService = pkgs.writeShellScript "airlock-config-inputs-import-qrexec" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" "@NIX_7@" "@NIX_OPTIONAL@" ]
      [
      "${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.gawk
        pkgs.gnugrep
        pkgs.gnutar
        pkgs.gzip
        pkgs.jq
        pkgs.nix
        pkgs.util-linux
      ]
    }"
      "${lib.escapeShellArg cfg.user}"
      "${cfg.configInputsPath}"
      "${cfg.configNixStoreRoot}"
      "${lib.escapeShellArg configInputsPublicPath}"
      "${pkgs.util-linux}"
      "${pkgs.coreutils}"
      "${pkgs.gnused}"
      "${lib.optionalString hasConfigStagingRole ''
      run_staging_init() {
        ${pkgs.coreutils}/bin/env \
          HOME=/home/${lib.escapeShellArg cfg.user} \
          USER=${lib.escapeShellArg cfg.user} \
          LOGNAME=${lib.escapeShellArg cfg.user} \
          XDG_CACHE_HOME=/home/${lib.escapeShellArg cfg.user}/.cache \
          AIRLOCK_CONFIG_INPUTS_PATH=${lib.escapeShellArg configInputsPublicPath} \
          AIRLOCK_CONFIG_REFRESH_INPUTS=1 \
          PATH=${lib.escapeShellArg (roleInitPathFor "sys-config-staging")} \
          ${configStagingInit}/bin/airlock-config-staging-init
      }
      if [ "$(${pkgs.coreutils}/bin/id -un)" = ${lib.escapeShellArg cfg.user} ]; then
        run_staging_init
      else
        ${pkgs.util-linux}/bin/runuser -u ${lib.escapeShellArg cfg.user} -- \
          ${pkgs.coreutils}/bin/env \
            HOME=/home/${lib.escapeShellArg cfg.user} \
            USER=${lib.escapeShellArg cfg.user} \
            LOGNAME=${lib.escapeShellArg cfg.user} \
            XDG_CACHE_HOME=/home/${lib.escapeShellArg cfg.user}/.cache \
            AIRLOCK_CONFIG_INPUTS_PATH=${lib.escapeShellArg configInputsPublicPath} \
            AIRLOCK_CONFIG_REFRESH_INPUTS=1 \
            PATH=${lib.escapeShellArg (roleInitPathFor "sys-config-staging")} \
            ${configStagingInit}/bin/airlock-config-staging-init
      fi
      [ -f ${lib.escapeShellArg cfg.configRepoPath}/flake.lock ] && locked=true
    ''}"
      ]
      (builtins.readFile ./scripts/airlock-config-inputs-import-qrexec.sh)
  );
}
