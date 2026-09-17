{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.poolAdmission;
  poolArtifacts = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
  targets =
    if cfg.targets == [ ] then
      "__airlock_no_pool_admission_target__"
    else
      lib.concatStringsSep "|" cfg.targets;
  handler = pkgs.writeShellScript "airlock-pool-admit-qrexec" ''
    set -eu

    remote="''${QREXEC_REMOTE_DOMAIN:-}"
    target="''${QREXEC_SERVICE_ARGUMENT:-}"
    [ "$remote" = ${lib.escapeShellArg cfg.caller} ] || {
      printf 'airlock.Pool.Admit is restricted to %s\n' ${lib.escapeShellArg cfg.caller} >&2
      exit 1
    }
    case "$target" in
      ${targets}) ;;
      *)
        printf 'airlock.Pool.Admit target is not permitted: %s\n' "$target" >&2
        exit 1
        ;;
    esac
    exec ${poolArtifacts}/bin/airlock-pool-artifact receive \
      --expected-domain "$target" \
      --approved-by "$remote"
  '';
  policy = pkgs.writeTextDir "etc/qubes/policy.d/42-airlock-pool-admission.policy" ''
    # Managed by Airlock. This service admits an inactive guest store generation.
    ${lib.concatMapStringsSep "\n" (target: "airlock.Pool.Admit +${target} ${cfg.caller} dom0 allow") cfg.targets}
    airlock.Pool.Admit * @anyvm @anyvm deny
  '';
in
{
  options.airlock.poolAdmission = {
    enable = lib.mkEnableOption "sys-dev guest artifact admission into the shared pool";
    caller = lib.mkOption {
      type = lib.types.str;
      default = "sys-dev";
      description = "Qubes domain allowed to submit guest pool artifacts.";
    };
    targets = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Guest domains for which the caller may submit pool artifacts.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.targets != [ ];
        message = "airlock.poolAdmission.targets must not be empty when admission is enabled.";
      }
    ];
    environment.systemPackages = [ poolArtifacts ];
    environment.etc."qubes-rpc/airlock.Pool.Admit" = {
      source = handler;
      mode = "0555";
    };
    virtualisation.qubes.packages = [ policy ];
  };
}
