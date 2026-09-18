{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.poolAdmission;
  poolArtifacts = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
  targets =
    if cfg.targets == [ ] then
      "__airlock_no_pool_admission_target__"
    else
      lib.concatStringsSep "|" cfg.targets;
  mkRootReceiver = incremental: pkgs.writeShellScript "airlock-pool-admit-root${lib.optionalString incremental "-incremental"}" ''
    set -eu

    [ "$#" -eq 2 ] || {
      printf 'airlock.Pool.Admit root receiver requires target and caller\n' >&2
      exit 1
    }
    target="$1"
    approved_by="$2"
    [ "$approved_by" = ${lib.escapeShellArg cfg.caller} ] || {
      printf 'airlock.Pool.Admit root receiver rejected caller: %s\n' "$approved_by" >&2
      exit 1
    }
    case "$target" in
      ${targets}) ;;
      *)
        printf 'airlock.Pool.Admit root receiver rejected target: %s\n' "$target" >&2
        exit 1
        ;;
    esac
    exec ${poolArtifacts}/bin/airlock-pool-artifact receive${lib.optionalString incremental "-incremental"} \
      --expected-domain "$target" \
      --approved-by "$approved_by"
  '';
  mkHandler = receiver: pkgs.writeShellScript "airlock-pool-admit-qrexec" ''
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
    # VM-to-dom0 qrexec services run as dom0's normal control user. Admit
    # through a separately validated helper so only this pool operation gains
    # access to the root-owned closure store.
    exec /run/wrappers/bin/sudo -n ${receiver} "$target" "$remote"
  '';
  rootReceiver = mkRootReceiver false;
  incrementalReceiver = mkRootReceiver true;
  handler = mkHandler rootReceiver;
  incrementalHandler = mkHandler incrementalReceiver;
  policy = pkgs.writeTextDir "etc/qubes/policy.d/42-airlock-pool-admission.policy" ''
    # Managed by Airlock. This service admits an inactive guest store generation.
    ${lib.concatMapStringsSep "\n" (target: "airlock.Pool.Admit +${target} ${cfg.caller} dom0 allow") cfg.targets}
    airlock.Pool.Admit * @anyvm @anyvm deny
    ${lib.concatMapStringsSep "\n" (target: "airlock.Pool.AdmitIncremental +${target} ${cfg.caller} dom0 allow") cfg.targets}
    airlock.Pool.AdmitIncremental * @anyvm @anyvm deny
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
    environment.etc."qubes-rpc/airlock.Pool.AdmitIncremental" = {
      source = incrementalHandler;
      mode = "0555";
    };
    security.sudo.extraRules = [
      {
        users = [ config.airlock.dom0.user ];
        commands = [
          {
            command = "${rootReceiver}";
            options = [ "NOPASSWD" ];
          }
          {
            command = "${incrementalReceiver}";
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];
    virtualisation.qubes.packages = [ policy ];
  };
}
