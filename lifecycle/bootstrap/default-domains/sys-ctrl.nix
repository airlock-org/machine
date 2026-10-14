{
  guestHostName = "sys-ctrl";
  label = "black";
  rootImageSizeMiB = 2048;
  # Candidate history and the writable Nix store live in the Qubes private
  # volume, independently of the immutable guest root and shared dom0 pool.
  state.persistentSize = 34359738368;
  netvm = "sys-firewall";
  prefs = {
    autostart = true;
    guivm = "sys-gui";
    include_in_backups = true;
    # The first whole-machine release build was killed in the 4 GiB VM.
    memory = 8192;
    maxmem = 8192;
    vcpus = 2;
  };
  features = {
    "airlock-managed" = "1";
    "airlock-role" = "sys-ctrl";
  };
  services."network-manager" = false;
}
