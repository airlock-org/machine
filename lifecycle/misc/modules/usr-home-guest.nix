{ pkgs, ... }:
let
  applications = import ../lib/usr-home-applications.nix { inherit pkgs; };
  openUrl = pkgs.writeShellScript "airlock-open-browser-url" ''
    set -eu

    url="''${QREXEC_SERVICE_ARGUMENT:-}"
    case "$url" in
      http://*|https://*) ;;
      *)
        printf 'airlock.OpenUrl accepts only http and https URLs\n' >&2
        exit 2
        ;;
    esac

    exec ${pkgs.firefox}/bin/firefox --new-window "$url"
  '';
in
{
  imports = [ ./app-gui-guest.nix ];
  # The connected default personal VM owns applications, browsing and files.
  # /home and the Firefox profile live on its private Qubes volume.
  environment.systemPackages = [
    pkgs.firefox
  ] ++ map (application: application.package) applications;

  # A fixed, validated URL-opening RPC lets the sys-gui browser desktop entry
  # preserve URI activation without granting it a general shell in this VM.
  environment.etc."qubes-rpc/airlock.OpenUrl" = {
    source = openUrl;
    mode = "0555";
  };
  environment.etc."qubes/rpc-config/airlock.OpenUrl" = {
    text = "force-user='user'\n";
    mode = "0444";
  };
}
