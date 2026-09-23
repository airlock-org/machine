{
  cfg,
  pkgs,
  roleGit,
  configNix,
  configInputsPublicPath,
  configInputPathSubstitutionScript,
  initialRuntimeConfigRepoFilesScript,
  initialRuntimeConfigRepoGitAddArgs,
}:
rec {
  configVaultInit = pkgs.writeShellScriptBin "airlock-config-vault-init" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" "@NIX_7@" "@NIX_8@" "@NIX_9@" ]
      [
      "${cfg.configArchivePath}"
      "${configInputsPublicPath}"
      "${pkgs.coreutils}"
      "${initialRuntimeConfigRepoFilesScript}"
      "${configInputPathSubstitutionScript}"
      "${roleGit}"
      "${pkgs.jq}"
      "${initialRuntimeConfigRepoGitAddArgs}"
      "${configNix}"
      "${pkgs.gnutar}"
      ]
      (builtins.readFile ./scripts/airlock-config-vault-init.sh)
  );

  configVaultExportCurrent = pkgs.writeShellScriptBin "airlock-config-vault-export-current" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" ]
      [
      "${cfg.configArchivePath}"
      "${pkgs.coreutils}"
      "${configVaultInit}"
      "${pkgs.jq}"
      "${pkgs.gnutar}"
      ]
      (builtins.readFile ./scripts/airlock-config-vault-export-current.sh)
  );

  configCurrentExportService = pkgs.writeShellScript "airlock-config-current-export-qrexec" ''
    exec ${configVaultExportCurrent}/bin/airlock-config-vault-export-current
  '';

  configVaultArchiveCandidate = pkgs.writeShellScriptBin "airlock-config-vault-archive-candidate" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" ]
      [
      "${cfg.configArchivePath}"
      "${pkgs.coreutils}"
      "${pkgs.gnutar}"
      "${pkgs.gawk}"
      "${pkgs.jq}"
      "${roleGit}"
      "${configVaultInit}"
      ]
      (builtins.readFile ./scripts/airlock-config-vault-archive-candidate.sh)
  );

  configVaultArchiveDeployment = pkgs.writeShellScriptBin "airlock-config-vault-archive-deployment" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" ]
      [
      "${cfg.configArchivePath}"
      "${pkgs.coreutils}"
      "${pkgs.gnutar}"
      "${pkgs.gawk}"
      "${pkgs.jq}"
      "${configVaultInit}"
      "${roleGit}"
      ]
      (builtins.readFile ./scripts/airlock-config-vault-archive-deployment.sh)
  );

  configArchiveService = pkgs.writeShellScript "airlock-config-archive-candidate-qrexec" ''
    exec ${configVaultArchiveCandidate}/bin/airlock-config-vault-archive-candidate
  '';

  configDeploymentArchiveService = pkgs.writeShellScript "airlock-config-archive-deployment-qrexec" ''
    exec ${configVaultArchiveDeployment}/bin/airlock-config-vault-archive-deployment
  '';
}
