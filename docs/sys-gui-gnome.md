The default personal desktop runs GNOME Web, files and terminal applications in
connected `usr-home`, using `sys-firewall` for internet access. The separate
`usr-browser` VM is no longer part of the default topology. Firefox is not
installed by default; no browser data is imported from the retired VM.

# GNOME sys-gui milestone

Status: GNOME and forwarded Firefox/Calculator windows verified live on the
physical lab machine; declarative fixes still need a fresh-image boot check.

`usb-gui-lab with Xen` is the primary physical-GPU sys-gui experiment. It uses
the stock GNOME desktop from the pinned Nixpkgs package set, without additional
GNOME Shell extensions.

Hyprland + DankMaterialShell remains available at
`usb-gui-lab-hyprland with Xen`; the known-good XFCE fallback is
`usb-gui-lab-xfce with Xen`. The three choices share the same GPU assignment,
firmware preparation, input forwarding, guest kernel, and sys-gui role. They
differ only in the selected desktop implementation.

GNOME's ordinary host-facing convenience services are explicitly disabled in
sys-gui: NetworkManager, Bluetooth, Avahi, GNOME Remote Desktop, user sharing,
and Rygel. GNOME Initial Setup is also disabled: the image already has its
local user and login policy. Sys-gui still has no NetVM. This does not change
the existing Qubes input, GUI, or PCI assignment boundaries.

Build and provision:

```console
make usb-dev-image
make usb-dev-provision USB_SERIAL=YOUR_USB_SERIAL
```

## Cross-VM app display fixes (2026-09-15)

Firefox in `usr-browser` and Calculator in `usr-home` were verified through
their normal desktop launchers, with visible cursors and working resize/maximize.
The image configuration now captures the live fixes:

- Mutter uses the upstream Qubes Xwayland wrapper, which preloads `shmoverride`.
  Mutter embeds the executable path, so an Xorg-only wrapper is insufficient.
- `/var/log/qubes` is group-writable by `qubes` for the user's GUI daemons.
- The guest GUI service sets a short `ENV_PATH`: the upstream session launcher
  cannot accommodate Nix's long generated service PATH.
- The guest qrexec-fork-server autostart entry uses its Nix store executable,
  so normal app RPCs inherit the graphical session environment.
- `airlock.appGui.scale` sets GTK rendering and cursor scale before upstream
  session startup imports the environment into D-Bus/systemd and qrexec.

The personal-VM defaults currently select scale **3**, matching the tested
panel's Xwayland rendering scale at GNOME's **250%** fractional monitor scale.
This is a lab default, not automatic monitor/DPI synchronization. Override
`airlock.appGui.scale` in the guest configuration for other displays (the base
module defaults to 1). Firefox and Calculator text size was confirmed live.

GNOME's forwarded-window policy leaves all window chrome to applications. The
GUI daemon requests no outer title bar or frame and draws no colour border,
including on override-redirect and docked windows. All forwarded windows use
the same policy and remain managed normally. The trusted focused VM panel
indicator identifies only the focused window. Applications that depend on
external title-bar controls must supply those inside their VM. Manual
acceptance of move/resize/maximize, dialogs and login/lock remains pending.

## Capture delivery to usr-home

The connected GNOME desktop keeps capture in sys-gui, where Mutter composes
the visible application windows and GNOME Shell. A small GNOME Shell package
patch invokes `airlock-capture-transfer` after the common screenshot save path
closes its file, and after a screencast reaches EOS and its pipeline is torn
down. Screenshot UI controls and the direct screenshot shortcuts share that
save path. The early recording-state change does not trigger delivery.

The helper queues completed files and streams them through the dedicated
`airlock.CaptureImport+image` / `+video` qrexec services. The host GUI policy
permits only sys-gui to usr-home. The unprivileged receiver accepts a bounded
versioned metadata header and capture bytes, verifies their size and SHA-256,
and publishes the finished file without overwriting an existing file. Durable
receipts deduplicate retries, including a lost acknowledgement. It saves into
usr-home's XDG Pictures/Screenshots or Videos/Screencasts directories.

The native notifications' open and Show in Files actions use
`airlock.CaptureOpen` to open an already delivered capture in usr-home; they
do not accept an arbitrary remote path or command. Opening waits for the
application VM's existing graphical-session readiness barrier.

Delivery is event-triggered: there is no polling timer or directory watcher.
Login runs one retry pass and a one-time import of existing captures (existing
videos must pass ffprobe). A missing usr-home or failed delivery retains the
queue and local file; the next login or completed capture retries. Usr-home is
started by normal qrexec policy when needed. Original local files are retained
as recovery copies after delivery; automatic deletion is not implemented.
GUI-only profiles keep GNOME's local capture behaviour.

Capture permissions precede the GUI policy's default deny in
`50-airlock-sys-gui.policy`. Installing them requires activating the new host
generation; updating only the guests cannot grant these RPCs.

Implementation: `lifecycle/misc/modules/capture-transfer.nix`,
`lifecycle/misc/modules/sys-gui/capture-transfer.py`, and
`lifecycle/misc/patches/gnome-shell/0001-completed-capture-handoff.patch`.
Behavioural pytest coverage is in `tests/test_capture_transfer.py`.

## Deferred: appearance synchronization

Changing light/dark appearance in sys-gui does not currently update applications
running in `usr-home` or `usr-browser`, because each VM has its own GNOME/dconf
settings and session bus. Add a narrow appearance-sync mechanism later:

- watch sys-gui's `org.gnome.desktop.interface color-scheme` value;
- send only allowlisted appearance values over a dedicated qrexec service;
- restrict policy to sys-gui targeting `usr-home` and `usr-browser`;
- apply the value to the desktop user's GNOME settings in each target VM; and
- apply the current value when an application VM starts, as well as forwarding
  later changes to already-running applications.

Do not share the complete dconf database or a session bus between VMs. The same
mechanism may later gain separately allowlisted accent colour, high-contrast and
text-scaling values. Window-manager chrome already belongs to sys-gui and does
not need this synchronization.
