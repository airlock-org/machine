{
  cfg,
  lib,
  pkgs,
  roleGit,
  configNix,
  configInputsPublicPath,
  configInputPathSubstitutionScript,
  initialRuntimeConfigRepoFilesScript,
  initialRuntimeConfigRepoGitAddArgs,
  configValidatorValidateCandidate,
}:
rec {
  configStagingImportCurrent = pkgs.writeShellScriptBin "airlock-config-staging-import-current" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" ]
      [
      "${cfg.configRepoPath}"
      "${pkgs.coreutils}"
      "${pkgs.gnutar}"
      "${pkgs.gawk}"
      "${pkgs.jq}"
      "${roleGit}"
      "${pkgs.findutils}"
      ]
      (builtins.readFile ./scripts/airlock-config-staging-import-current.sh)
  );

  configCurrentImportService = pkgs.writeShellScript "airlock-config-current-import-qrexec" ''
    exec ${configStagingImportCurrent}/bin/airlock-config-staging-import-current
  '';

  configStagingInit = pkgs.writeShellScriptBin "airlock-config-staging-init" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" "@NIX_7@" "@NIX_8@" "@NIX_9@" "@NIX_10@" "@NIX_11@" ]
      [
      "${cfg.configRepoPath}"
      "${configInputsPublicPath}"
      "${initialRuntimeConfigRepoFilesScript}"
      "${configInputPathSubstitutionScript}"
      "${pkgs.gnugrep}"
      "${lib.escapeShellArg cfg.user}"
      "${cfg.configInputsPath}"
      "${roleGit}"
      "${configNix}"
      "${pkgs.coreutils}"
      "${pkgs.findutils}"
      "${initialRuntimeConfigRepoGitAddArgs}"
      ]
      (builtins.readFile ./scripts/airlock-config-staging-init.sh)
  );

  configStagingValidate = pkgs.writeShellScriptBin "airlock-config-staging-validate" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" "@NIX_7@" "@NIX_8@" ]
      [
      "${cfg.configRepoPath}"
      "${cfg.configValidationPath}"
      "${pkgs.coreutils}"
      "${roleGit}"
      "${pkgs.jq}"
      "${configStagingExportCandidate}"
      "${configValidatorValidateCandidate}"
      "${pkgs.gnutar}"
      "${pkgs.gawk}"
      ]
      (builtins.readFile ./scripts/airlock-config-staging-validate.sh)
  );

  configStagingExportCandidate = pkgs.writeShellScriptBin "airlock-config-staging-export-candidate" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" ]
      [
      "${cfg.configRepoPath}"
      "${pkgs.coreutils}"
      "${roleGit}"
      "${pkgs.jq}"
      "${pkgs.gnutar}"
      ]
      (builtins.readFile ./scripts/airlock-config-staging-export-candidate.sh)
  );

  configCandidateExportService = pkgs.writeShellScript "airlock-config-candidate-export-qrexec" ''
    exec ${configStagingExportCandidate}/bin/airlock-config-staging-export-candidate
  '';
}
