{
  guestHostName = "usr-home";
  label = "blue";
  rootImageSizeMiB = 1024;
  netvm = "sys-firewall";
  prefs = {
    # Reconcile an existing explicit guivm=None as well as fresh VM defaults.
    guivm = "sys-gui";
    # Keep the connected default personal VM ready alongside sys-gui. Its applications
    # own the user's files and shell state while their windows render in sys-gui.
    autostart = true;
    include_in_backups = true;
    memory = 2048;
    maxmem = 4096;
    vcpus = 2;
  };
  features = {
    "airlock-managed" = "1";
    "airlock-role" = "usr-home";
    "menu-items" = "*";
  };
  services."network-manager" = false;
  state.persistentSize = 17179869184;
}
