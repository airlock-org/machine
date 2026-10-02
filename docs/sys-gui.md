# sys-gui desktop and login

`sys-gui` owns LightDM's login screen, desktop session, Qubes Manager,
application menus, tray widgets, notifications, and graphical settings tools.
The connected `usr-home` personal VM owns Firefox, Files and Terminal processes
and the user's persistent home directory; their windows render in `sys-gui`.
Its default packages also include document, image, media, calendar, contact,
editor, utility and help applications. These packages supply their own desktop
entries; `sys-gui` has no personal-application list or package-specific launchers.
It does not install GNOME's broad core app set in the connected desktop profile.
The bootstrap and initial runtime configurations use **user / airlock** for
the guest login. The dom0 console account remains **airlock / airlock**.
These are development credentials. Deployments should configure the GUI
user through NixOS `users.users.user.hashedPasswordFile`; credentials are
independent of dom0 and are not copied out of its shadow file.

Dom0 automatically opens a greeterless relay session. It retains Xorg,
LightDM's session supervisor, and `qubes-guid` to forward the guest display.
The Qubes GUI agent starts the outer Xorg server on `:1` inside `sys-gui`;
the `qubes.GuiVMSession` RPC then starts guest LightDM on a software-cursor
Xephyr display. Guest LightDM authenticates the user and launches XFCE.
Logging out returns to the guest login screen. Disconnecting the relay
stops guest LightDM, so reconnecting starts at the greeter.

Dom0 no longer installs XFCE, its fallback xterm desktop, Qubes Manager,
the Qubes desktop-manager frontend package, or Qubes artwork in its system
profile. Xen, libvirt, qubesd, QubesDB, qrexec, and policy enforcement remain
in dom0. The desktop-linux-common package stays there because it also
contains RPC and command-line helpers used by the backend. Dependencies and
the guest closures shipped in the image can still contain GUI libraries;
this is not a claim that every GUI-related store path has been removed.

The desktop tools use the Qubes Admin API. Policy explicitly permits VM
inspection, creation of AppVM/StandaloneVM/TemplateVM domains, per-VM
settings, firewall rules, volume resizing, lifecycle actions, and removal.
Global settings writes, dom0 shell access, policy editing, host device
assignment, and backup/restore are not granted. Some installed tools or
controls will therefore report permission denied; installing a frontend
does not give it unrestricted host administration. Runtime VM edits can be
reconciled back to the declared Nix configuration by subsequent setup.

This remains a hybrid GUI domain. Removing dom0's Xorg server requires a
separate display-device and input-ownership change. The AI `sys-gpu` role
remains independent. `sys-config-*` VMs remain on demand in the full profile.

For a fresh full development image, build and boot with
`AIRLOCK_VM_FRESH=1 make vm-gui-config-run`. Verify guest login, visible cursor,
VM Manager, logout/relogin, and that config VMs remain halted. Guest login
diagnostics are in `journalctl -b -u display-manager` inside `sys-gui`.

## Applications across VMs

In GNOME, forwarded applications supply their own title bars. The trusted Qubes
GUI daemon requests no additional window-manager decorations. Forwarded windows
from VMs named `sys-*` have a two-pixel outline in their Qubes label colour
(for example, red for `sys-agent`). The daemon draws this on the outer pixels
of the forwarded window using the VM identity supplied by Qubes, independently
of application names or classes. Docked tray icons have no added outline.
Personal VM windows such as `usr-home` have no added outline; their native
application edges and GNOME shadows remain. The focused VM panel identifies
windows from every VM. Applications running directly in sys-gui are unaffected.

The desktop user's `airlock-application-menus.timer` starts two seconds after
the graphical session starts and repeats thirty seconds after each refresh
finishes. It obtains a complete Qubes inventory and discovers applications in
every VM whose `guivm` is `sys-gui`, excluding internal VMs, recoverably retired
VMs and transient automatically deleted disposables. No application or VM name
is hardcoded into discovery. A new VM must have a working Qubes GUI agent and
the standard app-menu RPCs; Nix application VMs import `app-gui-guest.nix`.

Each running VM exposes desktop entries from its active system/user profiles
and user-local XDG application directories. User entries take precedence,
including `Hidden` entries masking system apps. `NoDisplay`, `TryExec` and
desktop visibility metadata are respected. Old Nix generations and GC roots
are not searched. Switching a profile, installing or removing a package, or
editing a user desktop entry is picked up on a subsequent refresh without a
sys-gui rebuild. Applications must supply a normal `.desktop` entry.

Upstream Qubes code sanitizes the received metadata, fetches icons using the
existing RGBA RPC and generates per-VM entries in the ordinary user XDG
application directory. A small packaged patch retains search keywords,
localized names and desktop visibility fields, supports `menu-items = "*"`
for showing every discovered app, and gives application launchers stable IDs
containing the VM UUID and application desktop ID. Explicit per-VM menu lists
remain available through Qubes settings. The GUI's copy of an icon is a cache;
applications and their dependencies remain owned by the originating VM's Nix
profiles, with no additional GC roots created by the application menu.

GNOME search displays entries such as `usr-home: Firefox` and
`usr-work: Firefox`. Both launch through `qubes.StartApp` in the named VM,
preserving separate browser profiles and files. Standard `StartupWMClass`
metadata is namespaced using the VM name for forwarded-window matching.
Airlock guests advertise their session-readiness RPC; other Qubes guests use
upstream `qubes.WaitForSession`. Search does not boot stopped VMs. Selecting a
launcher can start its VM through the existing Qubes permissions.

The GNOME session includes the Airlock VM Search extension. Application search
uses full-name rows grouped under VM headings with their Qubes label colours;
stopped VMs keep their cached applications and show a `Stopped` heading hint.
Group order follows the highest-ranked GNOME match in each VM, and rows retain
GNOME's ordering within their group. Names wrap, headings do not take keyboard
focus, and the existing overview scroll view handles larger result sets without
the original six-tile limit hiding another VM's applications. Application icons
retain their original colours.

The extension reads the existing `airlock-appmenus/inventory.json` cache and
joins it to launcher UUIDs. It never derives VM identity or CSS colours from
guest-supplied names. Cache updates and application installation changes refresh
active searches. Failed cache reads preserve the last valid snapshot; unmatched
entries keep their full names under `Other applications`. Local applications
and system actions have separate groups. GNOME still owns matching, activation,
context menus, favorites and keyboard navigation. Disabling the extension restores
the normal application results display. Its display hooks target GNOME 50 and
need validation when upgrading Shell. The favorites dash and app grid use their
normal GNOME presentation.

## VM workspace visual prototype

The GNOME top panel shows contiguous workspace groups with VM names and Qubes
label colours. Numbered buttons switch workspaces; `+` adds an empty workspace
beside that VM's existing slots and switches to it. The active slot is highlighted.
Groups come from the same trusted inventory as application search, with a local
`sys-gui` group. A bounded horizontal scroll area keeps larger layouts accessible.

New normal application windows receive a one-time placement suggestion based on
the trusted Qubes window identity reader. They open in their VM's active or last
used slot. Existing windows are left in place when the extension starts. Window
movement remains unrestricted: overview dragging, keyboard shortcuts and sticky
windows work as usual. A yellow `!` marks a slot containing another VM's windows
or windows whose origin has not yet been identified. The focused-VM indicator
continues to show the actual focused window's origin. These groups are a visual
experiment and provide no isolation or enforcement beyond existing Qubes policy.

The extension temporarily uses static workspaces so empty slots survive. Its
layout is stored in the desktop user's `airlock-vm-workspaces/layout.json` beneath
the XDG state directory. UUIDs preserve groups across VM renames; stopped or absent
VMs retain their slots, with absent VMs labelled `Unavailable VM`. The prototype
supports adding and switching slots, with at most 36 total, and has no removal or
group-reordering controls yet. Disabling the extension restores the original
workspace preferences. GNOME 50 workspace and panel APIs need validation on
Shell upgrades. All code and packaging are confined to `sys-gui`; no guest GUI
agent, qrexec policy or host control authority changes are required.

Catalogs persist in sys-gui's normal Qubes app-menu cache. Stopped VMs retain
their last successfully synchronized entries. Failed inventory or catalog
requests preserve cached apps, while a successful empty snapshot withdraws
uninstalled apps. Deletion, retirement and GUI reassignment withdraw a VM's
entries; renaming regenerates cached targets while preserving launcher IDs.
New VMs publish their initial catalog on their first startup. There is no
build-time catalog seeding in this version. Workers have bounded timeouts and
independent per-VM locks; unchanged snapshots skip icon refresh until the
five-minute periodic icon check.

The shared `app-gui-guest.nix` configuration exposes Hicolor and Adwaita icon
assets in each application VM's active profile. Applications resolve their own
toolbar and action icons there, including in headless guests such as sys-agent;
these assets do not require a desktop session or per-application overrides.

`airlock.sysGuiGuest.applicationMenus.enable` controls discovery.
`airlock.sysGuiGuest.defaultApplicationVM` defaults to `usr-home` for web-link
opening; that VM chooses its own browser using normal MIME defaults. Setting
it to `null` keeps local applications in minimal desktop profiles. This is
independent of discovery in other attached VMs. Predefined desktop/panel copies
are no longer created; users can pin discovered entries directly in GNOME.

For diagnostics in sys-gui, use:

```console
systemctl --user status airlock-application-menus.timer
journalctl --user -u airlock-application-menus.service
airlock-sync-qube-appmenus VM
```

The last command explicitly starts the selected VM and forces a refresh. The
background service never autostarts a VM. Hardware acceptance should cover two
VMs with the same app, favorites/window grouping, install/removal, stopped-VM
launching, login persistence and VM retirement.

## Default ownership direction

`sys-gui` is the intended standard desktop domain. The GPU needed for the
desktop belongs to it, including the only GPU on a single-GPU machine.
`sys-gpu` is an optional, hardware-specific addition for spare internal or
external GPUs, never a dependency of the desktop. For this machine the target
is AMD integrated graphics in `sys-gui`, with the RTX 3090 reserved for a later
custom `sys-gpu` deployment.

## Minimal nested image

`nixosConfigurations.vm-gui-minimal` declares only `sys-gui` alongside dom0.
It reuses Xen, qubesd, QubesDB, qrexec, libvirt, the Qubes GUI transport and the
existing Nix-native guest builder. The full image profiles remain unchanged.

- XFCE and the current hybrid display/login path are retained. Dom0 still
  hosts the outer display; this image does **not** pass through the AMD GPU.
- The sole guest has no NetVM and no PCI assignments.
- Network, configuration, application and `sys-gpu` VM images are not selected
  as dependencies of this image. Their flake outputs remain available.
- Files/Firefox launchers, their panel bootstrap and their MIME defaults are
  omitted because `usr-home` does not exist here. The normal
  GUI guest keeps those launchers.
- A single boot setup unit applies and records the entire small topology;
  no deferred provisioning, network-provider startup or config workflow runs.
- The full development toolset and the proxy targeting `sys-firewall` are
  disabled. Existing Qubes administration tools/policy are retained.

The topology is defined in `lifecycle/bootstrap/gui-minimal.nix`; the nested
harness settings are in `lifecycle/image/profiles/vm-gui-minimal.nix`.

## Build and launch when ready

```sh
make vm-gui-minimal-image
AIRLOCK_VM_DISPLAY=gtk make vm-gui-minimal-run
```

The launcher uses its own `vm-gui-minimal` directory under the Airlock data
root, loopback SSH port 2332 and 8192 MiB RAM by default. Override
`VM_GUI_MINIMAL_DIR`, `VM_GUI_MINIMAL_SSH_PORT` or `AIRLOCK_VM_MEMORY` as needed.
It does not reuse the normal development VM disk. The minimal launcher sets
`AIRLOCK_VM_SYSNET_NIC=0`, omitting the extra NIC used by the full networked
profile. Other launch targets keep that NIC enabled by default.

In another terminal:

```sh
make vm-gui-minimal-ssh
sudo systemctl status airlock-setup-config
sudo qvm-ls
```

This is a disposable nested test image, not a release for physical machines:
dom0 retains DHCP/SSH for the test harness, the `airlock` account uses password
`airlock` and passwordless sudo, and the `sys-gui` login is `user` with password
`airlock`. Do not deploy these credentials or network settings on hardware.

Pytest configuration coverage is in `tests/test_gui_minimal.py` and included
in `make test-quick`. No tests, builds or VM boots were run for this change,
as requested. Physical display ownership and boot behavior remain unverified.

## Next hardware milestone

Keep the hybrid image as a development/recovery path. A separate, explicit
AMD direct-display configuration still needs:

1. GPU/IOMMU and reset checks, companion-function selection and boot-time
   reservation so dom0 does not load the GPU's normal display driver.
2. A compatible guest kernel, `amdgpu`, firmware and Mesa. Guests currently
   boot a shared Qubes kernel; a NixOS graphics option alone does not prove
   driver compatibility.
3. A direct display-manager path in `sys-gui`, independent of the existing
   dom0 GUI session relay, plus keyboard/mouse delivery at login.
4. A tested recovery boot entry before handing over the only display GPU.
5. XFCE login, locking, monitor modes and reboot validation on hardware.

Hyprland follows that milestone, with separate Qubes window/input integration
testing. No GPU discovery or automatic device reassignment is introduced here.

The opt-in physical laptop experiment now has a confirmed GUI login with
keyboard and pointer input. Its [reusable input module and findings](sys-gui-input.md)
are separate from machine-specific GPU reservation and firmware delivery.
This does not change the default hybrid profile or establish reset safety.
