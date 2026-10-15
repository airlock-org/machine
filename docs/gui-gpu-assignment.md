# GPU assignment for sys-gui

The optional physical-GPU profile connects a machine's GPU selection to
early Xen reservation, existing Qubes bootstrap, and one guest start. Firmware
delivery remains [a separate Qubes startup extension](amd-vbios-automatic.md).
Device selection and reset safety must be established for the configured hardware.

## Configuration boundary

`lifecycle/misc/modules/gui-gpu.nix` is reusable. A machine configuration supplies
`airlock.guiGpu.device`: the host PCI `bdf`, Qubes topology `port`, expected
four-digit `vendor` and `device` IDs, and dom0 `driver`. Declaring this inventory
alone does not reserve or assign anything. Enable ownership separately:

```nix
airlock.guiGpu = {
  enable = true;
  startOnBoot = true;
  # device = { ... }; supplied by the machine configuration
};
```

The device selection belongs in a hardware module under
`lifecycle/image/hardware/`. The physical `usb-gui-lab` profile enables ownership;
`usb-lab` imports that profile. Hybrid display and text recovery have separate
configurations. Device declarations must match the installed hardware.

The current module supports one segment-zero GPU for HVM `sys-gui`, with
`video-model=none`. The machine must not explicitly load its graphics driver
in the initrd when ownership is enabled. Unrelated early modules are retained.
Other vendor choices exercise the selection mechanism, not certified guest
driver/firmware support. The physical AMD guest still needs compatible VFCT.

## Boot sequence and checks

1. Nix generates `xen-pciback.hide=(BDF)`, loads xen-pciback early, blacklists
   the selected dom0 driver, disables the host firmware framebuffer by default, and
   requires Xen's IOMMU. It does not reserve sibling audio/USB/security devices.
2. Before bootstrap mutates VM/storage/settings, `airlock-gui-gpu-check` checks
   the PCI function exists, vendor/device and display class match, pciback owns
   it, and Qubes resolves the selected port to exactly that host BDF. It checks
   the generated topology agrees and rejects conflicting existing ownership,
   options or assignments. Its API calls and sysfs access are read-only.
3. The existing `airlock-setup-config` creates/configures sys-gui and applies
   its sole persistent **required**, port-matched Qubes assignment. Matching
   assignments are reused; stale or conflicting assignments are not silently
   removed. No reset/permissive options are supplied.
4. `airlock-start-gui-gpu.service` requires successful bootstrap, checks that
   `qubesd` is active, rechecks the hardware and persistent assignment, then
   runs ordinary `qvm-start sys-gui`
   once. Qubes/VFCT/libvirt perform the normal guest start. Qubes autostart is
   disabled for this VM so it cannot race this ordering; no retry loop or
   automatic guest restart is configured.

The physical profiles enable a temporary firmware console for bootstrap
progress. The startup precondition detaches that console and firmware driver
before `qvm-start`; failed release blocks the guest. This sole-display-GPU
experiment does not change the default reservation behavior for other users
of the module. See [the framebuffer handoff](https://github.com/airlock-org/airlock/blob/main/docs/history/usb-gui-lab.md#temporary-bootstrap-framebuffer).

Boot reservation is address-based and happens before userspace identity
validation. If hardware changes, a stale BDF can reserve the wrong function;
validation prevents assigning that mismatch to sys-gui, not the earlier
reservation. Review/regenerate the machine selection after hardware changes.
This is declarative application of a reviewed choice, not a hotplug manager.
Root can still change assignments or use Qubes commands manually; these checks
are boot-workflow safeguards, not a restriction on dom0 administrators.

Xen owns the IOMMU, so Linux dom0 `iommu_group` links are not a valid universal
precondition. Reset/isolation enforcement remains with Xen/Qubes/libvirt. No
ACS override, forced live unbind, `no-strict-reset`, or permissive flag is added.
Normal Qubes lifecycle operations can themselves reset the GPU.

## Profile switching and recovery

Persistent Qubes assignments survive reboot. An empty hybrid PCI declaration
does **not** delete the physical lab's assignment. On machines with declared
GPU inventory, the disabled profile's bootstrap guard rejects leftover
sys-gui PCI state instead of allowing the desktop relay to start it with dom0's
display GPU. Text recovery does not run bootstrap or start the guest.

To return to hybrid mode, boot text recovery, confirm sys-gui is halted, inspect
`qvm-device pci list --assignments`, and unassign the verified sys-gui GPU through
Qubes. Then reboot the default entry. Do not remove VM disks or `qubes.xml`.
Reflashing/reprovisioning also creates fresh runtime state, but is not necessary
just to remove a reviewed assignment.

Recorded boot and assignment experiments are in the
[GPU assignment record](history/gui-gpu-assignment-2026-10-15.md).
