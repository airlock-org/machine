{
  guestHostName = "sys-dev";
  label = "red";
  rootImageSizeMiB = 1024;
  netvm = "sys-firewall";
  prefs = {
    autostart = true;
    guivm = null;
    include_in_backups = true;
    # Whole-machine Nix evaluation exceeds 4 GiB RSS.
    memory = 8192;
    maxmem = 8192;
    provides_network = false;
    vcpus = 2;
    virt_mode = "hvm";
  };
  features = {
    "airlock-managed" = "1";
    "airlock-role" = "sys-dev";
    # Explicitly clear the prior provider features on deployed Qubes domains.
    "airlock-auto-network-pci" = "0";
    "airlock-network-provider" = "0";
  };
  services = {
    "network-manager" = false;
    "qubes-firewall" = false;
    "qubes-network" = false;
  };
  devices.pci = [ ];
  state.persistentSize = 17179869184;
}
