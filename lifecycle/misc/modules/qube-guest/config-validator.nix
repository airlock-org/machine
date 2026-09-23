{
  cfg,
  pkgs,
  roleGit,
  configNix,
}:
rec {
  configValidatorInit = pkgs.writeShellScriptBin "airlock-config-validator-init" ''
    set -eu

    validation_dir="''${AIRLOCK_CONFIG_VALIDATION_DIR:-${cfg.configValidationPath}}"
    mkdir -p "$validation_dir/logs"
    printf 'config validator workspace ready: %s\n' "$validation_dir"
  '';

  configValidatorValidateCandidate = pkgs.writeShellScriptBin "airlock-config-validator-validate-candidate" (
    pkgs.lib.replaceStrings
      [ "@NIX_0@" "@NIX_1@" "@NIX_2@" "@NIX_3@" "@NIX_4@" "@NIX_5@" "@NIX_6@" "@NIX_7@" ]
      [
      "${cfg.configValidationPath}"
      "${pkgs.coreutils}"
      "${pkgs.gnutar}"
      "${pkgs.gawk}"
      "${pkgs.jq}"
      "${roleGit}"
      "${configNix}"
      "${pkgs.gnused}"
      ]
      (builtins.readFile ./scripts/airlock-config-validator-validate-candidate.sh)
  );

  configCandidateValidateService = pkgs.writeShellScript "airlock-config-candidate-validate-qrexec" ''
    exec ${configValidatorValidateCandidate}/bin/airlock-config-validator-validate-candidate
  '';
}
