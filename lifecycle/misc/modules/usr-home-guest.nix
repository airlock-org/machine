{ pkgs, ... }:
{
  imports = [ ./app-gui-guest.nix ./capture-transfer.nix ];
  airlock.captureTransfer.receive.enable = true;
  # Only choose packages here. Discovery, names, icons and launching use the
  # installed packages' standard desktop entries in every application VM.
  environment.systemPackages = [ pkgs.firefox ]
    ++ import ../lib/usr-home-applications.nix { inherit pkgs; };
}
