# Personal GNOME application environment

`usr-home` follows the GNOME core application defaults from Airlock's pinned
NixOS release through `services.gnome.core-apps.enable`. GNOME Web (Epiphany)
is the browser; Firefox is not installed by this role. Upstream desktop entries
provide application discovery, names and icons through the existing Qubes
application menu path. Loupe appears as **Image Viewer**.

This role opts out of the minimal guest profile. Normal NixOS documentation,
manual pages, Nano, shell completion and default convenience packages are
restored. The upstream command-not-found default still depends on availability
of its package database; a GitHub-pinned nixpkgs input does not supply that
database. Every other VM retains its existing minimal configuration and
role-specific software. This change does not reduce those VMs' footprints.

The application VM provides GVfs, DConf, accessibility, calendar/address-book
services, online accounts, search, keyring storage, thumbnails and a GTK desktop
portal. HTTP, HTTPS and HTML open in GNOME Web; common image types open in
Loupe, PDFs in Papers, text files in Text Editor and directories in Files.
The portal uses the X-QUBES session and does not require another GNOME Shell.
The standard NixOS graphics module supplies Mesa/EGL for application software
rendering. Qubes supplies the X server, and no physical GPU is assigned to
`usr-home`.

GNOME Shell, login, display and physical hardware management remain in
`sys-gui` and the existing service VMs. System Monitor and Disks operate on the
application VM's own resources. Qubes agents, networking through
`sys-firewall`, window forwarding, capture reception and persistent private
storage remain unchanged. GNOME Web uses the persistent user home; old Firefox
profile data is not deleted or migrated automatically.

Applications are installed through the committed machine configuration and
controller deployment. Nix, its daemon/channels, NixOS installer tools and
Flatpak are disabled in `usr-home`; no private package builder is added. This
is the package-management boundary for the current milestone, not a restriction
on executing user files.

An update to this guest module can use the documented controller single-guest
plan/apply route. Inspect the plan before activation: only `usr-home` should
change or restart, with the host and `usr-assistant` preserved. Verify the entire
machine selection afterwards and check application discovery, file opening and
persistence separately from generation equality.
