# Automatic AMD VBIOS delivery

Airlock can prepare an AMD GPU's VBIOS from the booted host's ACPI VFCT table
during ordinary Qubes VM startup. There is no laptop address, product ID,
pre-extracted firmware file, or source-table hash in the configuration.

The opt-in Nix module is `lifecycle/misc/modules/amd-vbios.nix`:

```nix
airlock.amdVbios = {
  enable = true;
  domains.sys-gui = { }; # guestSlot defaults to 16 (PCI slot 0x10)
};
```

The physical GPU profiles enable it. The hybrid display profile does not. Device
reservation, assignment, and the decision about isolation/reset safety remain
separate: the lab still reserves its reviewed GPU through its hardware boot
configuration. Another machine must supply its own reservation and input
selection. This feature does not discover which GPU the user wants to assign.
The GPU module connects that choice to boot using
[declarative sys-gui assignment](gui-gpu-assignment.md); firmware preparation
itself still does not assign devices.

## Startup behavior

1. Qubes' normal `qvm-start` runs the extension's pre-start validation. The
   opted-in domain must have exactly one required PCI assignment from dom0.
   The extension resolves that assignment through Qubes, reads the device's
   actual PCI identity in sysfs, and requires an AMD display controller already
   reserved to pciback. It never binds, assigns, or resets hardware itself.
2. Before libvirt creates the guest, the extension reads the current host
   `/sys/firmware/acpi/tables/VFCT`. The shared validator selects exactly one
   image matching the GPU's host PCI address and vendor/device IDs. It checks
   ACPI lengths/checksum, image boundaries, ROM checksum/length, ATOM header,
   and PCIR identity. Ambiguous or unsupported tables stop startup.
3. It constructs a filtered VFCT containing only that image. The VBIOS ROM
   bytes remain unchanged. The table's PCI address is changed to the chosen
   guest slot, and its ACPI checksum is recomputed. A new root-only directory
   under `/run/airlock-amd-vbios/` contains `vfct.bin` and `manifest.json` for
   that attempt. No previous prepared table is reused.
4. A per-domain template renders the upstream Qubes Xen XML. The extension
   preserves that definition and adds only the matching hostdev's explicit
   guest PCI address and the generated VFCT via the existing ACPI transport.
   Conflicting custom ACPI tables, addresses, or device identities are errors.
5. The extension checks libvirt's XML-to-XL conversion before creation. It
   requires the explicit guest slot and the generated table path to survive
   conversion; an old libvirt daemon cannot silently discard the slot. Qubes
   then performs its normal VM creation and PCI assignment.

The firmware hash records provenance and detects changes, but does not
authenticate the manufacturer's firmware. Structural validation is not a
claim that firmware is harmless. Raw firmware stays on the machine; manifests
and the journal contain identities, lengths, and hashes.

Pre-start and hotplug guards use Qubes instance handlers, which run before
class-level PCI extension handlers for pre-events. Attaching/detaching PCI
devices while an opted-in VM is running is rejected. Removing a persistent
assignment without detaching the running device remains available for the lab.
Failed/stopped starts clear the in-memory prepared state; artifacts remain in
the root-only runtime directory for diagnosis until reboot. Resuming saved VM
state is outside this workflow; the USB lab disables managed save/auto-restore.

## Why a libvirt patch is needed

Pinned libvirt 10.5's `libxlMakePCI` passed the host address to Xen but omitted
the requested guest address. Guessing QEMU's automatic allocation would make
firmware identity depend on the device layout.

Airlock's `0020-libxl-honor-explicit-guest-pci-slot.patch` passes an explicit
guest slot to existing Xen `vdevfn` support and preserves it in XML/XL
roundtrips using `host-address@slot`. It rejects unsupported guest addresses.
Devices without an explicit address retain automatic placement. No Xen patch
is added, and no reset, permissive, or reserved-memory security flags are changed.

Source references: [pinned libvirt PCI conversion](https://github.com/libvirt/libvirt/blob/v10.5.0/src/libxl/libxl_conf.c#L2102),
[Qubes startup events](https://github.com/QubesOS/qubes-core-admin/blob/v4.3.44/qubes/vm/qubesvm.py),
[Qubes event ordering](https://github.com/QubesOS/qubes-core-admin/blob/v4.3.44/qubes/events.py).
The old `slic` spelling is the pinned Xen driver's raw ACPI-table transport;
see [the original VFCT investigation](amd-gpu-vbios.md).

## Supported boundary

- One required, segment-zero AMD display-controller assignment from dom0.
- A compatible VFCT with one unambiguous matching ATOM/PCIR ROM image.
- Qubes HVM, SeaBIOS, Linux stubdom, `video-model=none`, and no custom QEMU
  arguments. No VFCT library section, multiple GPU images for the same device,
  or alternative firmware acquisition mechanism is supported yet.
- Explicit guest bus zero/function zero, slot 8–31 (default 16). This is a
  virtual machine layout contract, not a host hardware address.
- Firmware delivery at normal VM creation; no GPU hotplug, live migration,
  managed-save resume, or reset-safety certification.

Other vendors, missing VFCT, unsupported table layouts, absent devices, and
conflicting VM layouts produce an error through Qubes. Firmware validation does
not establish device reset safety.

## Implementation checks

Firmware validation and runtime tests are in `tests/test_amd_vbios.py` and
`tests/test_amd_vbios_runtime.py`. Follow [contributor setup](../CONTRIBUTING.md)
for local checks. Physical results and the original staged procedure are retained
in the [firmware investigation record](history/amd-vbios-automatic-2026-10-15.md).
