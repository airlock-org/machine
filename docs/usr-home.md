# Personal GNOME application environment

`usr-home` owns personal applications and the user's persistent home directory.
Its application selection and MIME defaults are defined in the
[guest module](../lifecycle/misc/modules/usr-home-guest.nix), using the pinned
NixOS GNOME defaults as a base. Upstream desktop entries provide application
discovery, names and icons through the existing Qubes application menu path.

This role opts out of the minimal guest profile to provide the ordinary NixOS
user environment. The upstream command-not-found default still depends on its
package database; a GitHub-pinned nixpkgs input does not supply that database.
Other VM roles retain their own minimal profiles and software selections.

Application settings, desktop integration and file opening are handled within
the application VM through standard GNOME/XDG mechanisms. The desktop portal
uses the X-QUBES session and does not require another GNOME Shell.
The standard NixOS graphics module supplies Mesa/EGL for application software
rendering. Qubes supplies the X server, and no physical GPU is assigned to
`usr-home`.

GNOME Shell, login, display and physical hardware management remain in
`sys-gui` and the existing service VMs. Application-level resource and storage
tools operate on the application VM's own resources. Qubes agents, networking
through `sys-firewall`, window forwarding, capture reception and persistent private
storage remain unchanged. Application data lives in the persistent user home;
removing an application does not automatically delete or migrate its data.

Applications are installed through the committed machine configuration and
controller deployment. Nix, its daemon/channels, NixOS installer tools and
Flatpak are disabled in `usr-home`; no private package builder is added. This
is the package-management boundary for the current milestone, not a restriction
on executing user files.

Routine package-list edits do not require documentation updates unless they
change documented behavior, instructions or supported capabilities. The Nix
configuration owns package selections; Git and deployment records track changes.

An update to this guest module can use the documented controller single-guest
plan/apply route. Inspect the plan before activation: only `usr-home` should
change or restart, with the host and `usr-assistant` preserved. Verify the entire
machine selection afterwards and check application discovery, file opening and
persistence separately from generation equality.
