# GNOME integration

The connected sys-gui configuration uses GNOME from the pinned Nixpkgs input.
The [GNOME module](../lifecycle/misc/modules/sys-gui/desktop/gnome/default.nix)
installs the focused-VM, VM search, and VM workspace extensions. Their source
lives alongside the module. See the [desktop guide](sys-gui.md) for user behavior.

## Qubes GUI session

Mutter uses the Qubes Xwayland wrapper with `shmoverride` for forwarded application
windows. The GUI agent starts the application VM's graphical session, and
qrexec application RPCs inherit that session's environment.

Sys-gui runs `qvm-start-daemon --all --watch` through the user service
`airlock-gui-daemons.service`. The service follows the GNOME session and restarts
if the watcher exits. The duplicate XDG autostart is disabled. Its user journal
is the first place to inspect when applications await a GUI connection.

Application rendering scale is configured per VM with `airlock.appGui.scale`.
It is not automatically synchronized with sys-gui's monitor scale or appearance
settings. The [application GUI module](../lifecycle/misc/modules/app-gui-guest.nix)
provides the shared application-session configuration.

## Capture delivery

A GNOME Shell patch invokes `airlock-capture-transfer` after a screenshot file
closes or a screencast reaches EOS and its pipeline is torn down. The helper
queues completed files and uses `airlock.CaptureImport+image` or `+video` to
stream a bounded metadata header and the capture to usr-home.

The receiver verifies size and SHA-256, publishes without overwriting existing
files, and records receipts to deduplicate retries. It saves to the user's XDG
Pictures/Screenshots and Videos/Screencasts directories. Open and Show in Files
use `airlock.CaptureOpen`, which accepts delivered capture identities rather than
arbitrary remote paths or commands.

Delivery is event-triggered. Login retries queued work and imports existing
captures once; existing videos must pass ffprobe. Failed delivery retains the
queue and local file. The next login or completed capture retries, starting
usr-home through the existing Qubes policy when needed. Local originals are
retained after delivery. GUI-only profiles keep GNOME's local capture behavior.

Capture RPC policy permits sys-gui to target usr-home. Activating that policy
requires the dom0 generation; a VM-only software update does not grant it.
Implementation is in [capture-transfer.nix](../lifecycle/misc/modules/capture-transfer.nix),
[the transfer helper](../lifecycle/misc/modules/sys-gui/capture-transfer.py), and
the [GNOME Shell patch](../lifecycle/misc/patches/gnome-shell/0001-completed-capture-handoff.patch).
[Behavior tests](../tests/test_capture_transfer.py) exercise delivery and retries.
