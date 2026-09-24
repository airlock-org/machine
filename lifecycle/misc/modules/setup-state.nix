{ config, lib, pkgs, ... }:
let
  state = import ../../runtime/tools/pkgs/airlock-setup-state { inherit pkgs; };
  handler = operation: pkgs.writeShellScript "airlock-setup-${operation}-rpc" ''
    set -eu
    [ "''${QREXEC_REMOTE_DOMAIN:-}" = sys-gui ] || exit 1
    [ -z "''${QREXEC_SERVICE_ARGUMENT:-}" ] || exit 1
    exec /run/wrappers/bin/sudo -n ${state}/bin/airlock-setup-${operation}
  '';
  policy = pkgs.runCommand "airlock-setup-policy" { } ''
    install -d "$out/etc/qubes/policy.d"
    cat > "$out/etc/qubes/policy.d/40-airlock-setup.policy" <<'EOF'
    airlock.SetupStatus + sys-gui dom0 allow
    airlock.SetupStatus * @anyvm @anyvm deny
    airlock.SetupComplete + sys-gui dom0 allow
    airlock.SetupComplete * @anyvm @anyvm deny
    EOF
  '';
in
{
  config = lib.mkIf (config.airlock.dom0.enable && config.airlock.dom0.gui.enable) {
    systemd.tmpfiles.rules = [ "d /var/lib/airlock/setup 0700 root root - -" ];
    environment.etc."qubes-rpc/airlock.SetupStatus" = {
      source = handler "status";
      mode = "0555";
    };
    environment.etc."qubes-rpc/airlock.SetupComplete" = {
      source = handler "complete";
      mode = "0555";
    };
    security.sudo.extraRules = [ {
      users = [ config.airlock.dom0.user ];
      commands = map (operation: {
        command = "${state}/bin/airlock-setup-${operation}";
        options = [ "NOPASSWD" ];
      }) [ "status" "complete" ];
    } ];
    virtualisation.qubes.packages = [ policy ];
  };
}
