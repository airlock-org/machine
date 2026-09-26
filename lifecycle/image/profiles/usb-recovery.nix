{ pkgs, ... }:
let
  storage = pkgs.python3.pkgs.callPackage ../../runtime/tools/pkgs/airlock-qubes-storage { };
in {
  imports = [ ../../misc/modules/boot-diagnostics.nix ];
  airlock.bootDiagnostics.enable = true;
  # Independent system: no parent topology, desktop, guests or main pool mount.
  virtualisation.xen.enable = true;
  boot.initrd.systemd.enable = true;
  boot.initrd.availableKernelModules = [ "nvme" "xhci_pci" "usb_storage" "uas" "sd_mod" "ahci" ];
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;
  boot.kernelParams = [ "console=tty0" "console=hvc0" ];
  fileSystems."/" = { device = "/dev/disk/by-label/airlock-usb"; fsType = "ext4"; };
  fileSystems."/boot" = { device = "/dev/disk/by-label/ESP"; fsType = "vfat"; };
  fileSystems."/nix/store" = {
    device = "/var/lib/airlock/recovery-store";
    fsType = "none";
    options = [ "bind" "ro" ];
    neededForBoot = true;
  };
  environment.etc."airlock/boot-role".text = "recovery\n";
  environment.etc."airlock/pool-recovery".text = ''
    Independent pool recovery. Networking and guests are disabled.
    Select a retained boot entry with: sudo airlock-host-store recover SYSTEM
  '';
  environment.systemPackages = [ storage pkgs.e2fsprogs pkgs.util-linux pkgs.lvm2 ];
  networking.useDHCP = false;
  networking.wireless.enable = false;
  users.mutableUsers = false;
  users.users.user = {
    isNormalUser = true;
    uid = 1000;
    extraGroups = [ "wheel" ];
    hashedPasswordFile = "/var/lib/airlock/usb/local-password.hash";
  };
  users.users.root.hashedPassword = "!";
  security.sudo.wheelNeedsPassword = true;
  # Recovery selects already-installed EFI artifacts, so it needs no daemon
  # and must not open or collect the main host's persistent Nix database.
  nix.enable = false;
  system.stateVersion = "26.05";
}
