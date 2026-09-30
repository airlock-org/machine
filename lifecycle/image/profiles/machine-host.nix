{ lib, pkgs, ... }:
{
  imports = [
    ../../bootstrap/gui-minimal.nix
    ./dev-mode.nix
  ];

  airlock.dom0 = {
    enable = true;
    user = "user";
    gui.enable = true;
    bootMenu.enable = true;
    devModeNetworkProxy.enable = false;
    dom0Resources = { memoryMiB = 8192; maxMemoryMiB = 8192; };
  };
  users.mutableUsers = false;
  users.users.user = {
    isNormalUser = true;
    uid = 1000;
    extraGroups = [ "wheel" "qubes" "networkmanager" ];
    hashedPasswordFile = "/var/lib/airlock/usb/local-password.hash";
  };
  users.users.root.hashedPassword = "!";
  security.sudo.wheelNeedsPassword = true;

  networking.useDHCP = false;
  networking.networkmanager.enable = true;
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 22 ];
  services.tailscale.enable = true;
  airlock.devDom0Terminal.host.enable = true;
  airlock.bootDiagnostics.enable = true;
  airlock.devDom0Terminal.user = "user";
  services.openssh = {
    enable = true;
    openFirewall = false;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
      AuthorizedKeysFile = lib.mkForce ".ssh/authorized_keys";
    };
  };
  services.udisks2.enable = lib.mkForce false;
  services.gvfs.enable = lib.mkForce false;
  services.usbguard.enable = lib.mkForce false;
  services.xserver.enable = true;
  services.xserver.displayManager.lightdm.enable = true;
  services.libinput.enable = true;

  boot.loader.systemd-boot.enable = true;
  # Try a denser firmware menu to bring its text closer to the startup console.
  # The 80x25 mode was visibly too large on the development laptop.
  boot.loader.systemd-boot.consoleMode = "1";
  boot.loader.systemd-boot.configurationLimit = 5;
  boot.loader.timeout = 10;
  boot.loader.efi.canTouchEfiVariables = false;
  boot.initrd.systemd.enable = true;
  boot.kernelParams = [ "console=tty0" "console=hvc0" ];
  boot.growPartition = false;
  fileSystems."/".device = lib.mkForce "/dev/disk/by-label/airlock-usb";
  fileSystems."/".fsType = "ext4";
  fileSystems."/".autoResize = lib.mkForce false;
  fileSystems."/boot".device = lib.mkForce "/dev/disk/by-label/ESP";
  fileSystems."/boot".fsType = "vfat";

  services.journald.settings.Journal = {
    Storage = "persistent";
    SystemMaxUse = "1G";
    SyncIntervalSec = "1s";
  };
  environment.systemPackages = with pkgs; [
    curl jq gnumake python3 cryptsetup rsync xdotool e2fsprogs openssl
  ];
  systemd.tmpfiles.rules = [
    "d /var/lib/airlock/usb 0700 root root - -"
    "d /home/user/dev 0755 user users - -"
    "d /home/user/.airlock-logs 0700 user users - -"
  ];
  systemd.services."qubes-vm@".enable = lib.mkForce false;
  system.stateVersion = "26.05";
}
