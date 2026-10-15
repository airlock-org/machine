# Image configuration

`profiles/` contains physical-machine and nested-VM image profiles. `hardware/`
contains machine-specific device configuration. The machine flake composes
these with dom0 and VM modules; deployment updates those systems from committed
configuration.

## Image building

Bootable disk assembly, provisioning, and nested-VM launch commands belong to
the parent airlock repository. Its
[image guide](https://github.com/airlock-org/airlock/blob/main/docs/usb-dev.md)
and [testing guide](https://github.com/airlock-org/airlock/blob/main/docs/testing.md)
describe those workflows. The component's [build instructions](../../CONTRIBUTING.md#nix-builds)
cover independently buildable machine releases.

Nested configurations have explicit development and release variants, including
`airlock-vm-gui-config-dev-image` and `airlock-vm-gui-config-release-image`.
Development profiles enable test access and diagnostics; release variants omit
those conveniences. These nested profiles are separate from the physical
machine's hardware and boot configuration.

Image-only tools live in `tools/`; helpers shared with bootstrap or runtime live
in `../misc/tools/`.
