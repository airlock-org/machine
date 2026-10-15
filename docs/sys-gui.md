# GNOME desktop in sys-gui

Sys-gui runs GNOME and displays applications from other VMs through Qubes GUI
agents. Personal applications and files live in [usr-home](usr-home.md);
configuration and administration use [usr-assistant](usr-assistant.md).
The connected machine configuration selects GNOME. Nested display-relay and
other desktop experiments have separate configurations.

## Applications across VMs

GNOME search groups applications by VM, retaining each application's name and
icon. Launchers such as `usr-home: Web` run the application in the named VM.
Stopped VMs retain cached launchers; searching does not start them. Selecting a
launcher can start its VM through the existing Qubes permissions.

The application-menu service refreshes entries from VMs assigned to sys-gui.
It reads standard desktop metadata from active system and user profiles,
respecting hidden entries and user overrides. Removed, retired, or reassigned
VMs lose their entries. A failed refresh preserves the last valid catalog.
Applications and dependencies remain in their originating VM.

The [sys-gui module](../lifecycle/misc/modules/sys-gui/base.nix)
and [catalog helper](../lifecycle/misc/modules/sys-gui/application-menus.py)
implement discovery. New NixOS application VMs use
[app-gui-guest.nix](../lifecycle/misc/modules/app-gui-guest.nix).

## VM identity and window borders

Forwarded applications provide their own title bars. Trusted Qubes VM identity
supplies the window border color and focused-VM panel indicator. Application
names and window titles do not determine VM identity.

The border width is declared in
[gui-style.json](../lifecycle/misc/modules/sys-gui/desktop/gnome/gui-style.json).
Forwarded windows use the selected width plus a black outer edge; tray icons
are excluded. Native application chrome and GNOME shadows remain.

## VM workspaces

The GNOME extension groups workspaces by VM and shows numbered tabs with VM
label colors. Clicking a tab switches workspaces; hover and keyboard focus show
the VM name and workspace number. Launching an application selects or creates
a workspace for its VM.

Windows can move within their VM's group. Drops into another VM's group are
rejected. Unknown window origins use a neutral workspace rather than inheriting
another VM's label. Dialogs follow their same-VM parent.

Workspace grouping is a GNOME extension behavior. Xen and Qubes provide VM
isolation; disabling the extension removes its workspace rules. Implementation
is in the [workspace extension](../lifecycle/misc/modules/sys-gui/desktop/gnome/vm-workspaces-extension/).

## Screenshots and screencasts

GNOME captures the composed desktop in sys-gui. Completed captures are delivered
to usr-home's Pictures/Screenshots or Videos/Screencasts directory. Open and
Show in Files actions operate on the delivered file in usr-home. Interrupted
delivery is retried, and local originals remain available in sys-gui.
See [capture integration](sys-gui-gnome.md#capture-delivery) for the protocol and
retry behavior.

## Application preservation during a sys-gui update

Unchanged application VMs with the trusted `airlock-gui-reconnect=1` feature can
remain running while their assigned sys-gui restarts. The default usr-home and
usr-assistant declarations enable this with the patched GUI agent and Xorg
modules. Updating an application VM itself still restarts it.

The agent retains application windows across GUI disconnection and reconnects
to the trusted sys-gui identity in QubesDB. Deployment records and verifies
preserved VM boot IDs. Reconnection waits for the new GUI session, including
login; a failed reconnect is reported without automatically restarting the
application VMs. Prior workspace positions and focus are not persisted.

Read the deployment plan to see which VMs are preserved and which restart.
[The workspace guide](usr-assistant.md) describes applying and verifying updates.

## Configuration and implementation

The [sys-gui base module](../lifecycle/misc/modules/sys-gui/base.nix) supplies
Qubes GUI integration. The
[GNOME module](../lifecycle/misc/modules/sys-gui/desktop/gnome/default.nix) configures
the desktop and extensions. [GNOME integration](sys-gui-gnome.md) describes
session services and capture transport. GPU ownership and physical input are
covered by [GPU assignment](gui-gpu-assignment.md) and [input forwarding](sys-gui-input.md).

The [archived desktop notes](history/sys-gui-2026-10-15.md) retain the earlier
display relay, desktop experiments, and detailed implementation observations.
