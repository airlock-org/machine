# Machine configuration and storage

The machine configuration is a Nix flake defining dom0 and the system and user
VMs. The installed checkout lives at `~/airlock-machine` in usr-assistant.
[The workspace guide](usr-assistant.md) describes editing and deploying it.

## Configuration entry points

- [flake.nix](../flake.nix) composes machine configurations and release outputs.
- [Default VM declarations](../lifecycle/bootstrap/default-domains/) define VM roles, networking, preferences, and private storage.
- [NixOS modules](../lifecycle/misc/modules/) define software, services, and integration.
- [Hardware modules](../lifecycle/image/hardware/) supply machine-specific device configuration.
- [qubes-topology.nix](../lifecycle/misc/modules/qubes-topology.nix) defines the topology options and renders the configuration used by setup.

The main VM roles are dom0, sys-gui, sys-net, sys-firewall, sys-ctrl, usr-home,
and usr-assistant. Xen runs all VMs. Qubes retains VM lifecycle, device ownership,
QubesDB, libvirt, qrexec, and policy. Nix defines the intended configuration;
Qubes runtime state, including `qubes.xml`, remains part of the running system.

## Configuration and deployment

Usr-assistant holds the editable checkout. `airlock machine plan` submits its
committed source to sys-ctrl. [Source approval](controller-review.md) occurs
before evaluation and building. Sys-ctrl checks the source revision and lock
hash, builds the release, and submits artifacts through the existing Qubes
interfaces. Dom0 validates admission and coordinates activation.

A release records the source revision, lock hash, VM topology, software, and
setup and deployment programs. Planning does not activate the release. Apply
runs in a detached dom0 worker; status and verification establish the resulting
machine state. Updates to dom0 can require a reboot.

The [deployment module](../lifecycle/misc/modules/machine-deployment.nix) defines
controller policy and interfaces. The
[coordinator implementation](../lifecycle/runtime/tools/airlock_machine_deploy/README.md)
describes journalling, admission, activation, and recovery.

## Shared global Nix store

Dom0 manages a shared global Nix store backed by the machine's storage pool.
VM system closures select the store paths each VM needs. Package objects shared
by closures are stored once; each VM receives a read-only view of its declared
closure. VM system generations refer to those immutable objects.

Private VM volumes hold home directories, credentials, application state, and
other mutable data. They are separate from system generations. System rollback
restores software and configuration while private data stays at its current state.

The build stores in usr-assistant and sys-ctrl combine shared read-only inputs
with private writable state. They do not grant those VMs authority to publish
or activate dom0's store. The installed Nix wrappers select the intended builder.

Storage implementation is split across the [dom0 store module](../lifecycle/misc/modules/host-store.nix),
[artifact admission](../lifecycle/misc/modules/pool-admission.nix),
[workspace builder](../lifecycle/misc/modules/usr-assistant-build-store.nix), and
[storage package](../lifecycle/runtime/tools/pkgs/airlock-qubes-storage/).

## Earlier configuration prototype

The repository retains internal helpers and tests for a staged configuration
prototype using sys-config-staging, sys-config-validator, and sys-config-vault.
Its old public CLI has been removed. Its dom0 RPCs are gated by
`airlock.qubes.configAccessPolicy.enable`; supplied bootstrap profiles disable
them. Installed-machine changes use usr-assistant and sys-ctrl.

The [archived design](history/config-system-2026-10-15.md) preserves the earlier
prototype, storage implementation notes, and experimental procedures.
