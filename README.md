# Airlock Machine

`airlock-machine` is the independently buildable whole-machine source for
Airlock. It owns the whole-machine Nix configuration, declared dom0 and Qubes
VM systems, runtime services and packages, bootstrap configuration, and the
tests that maintain those components. It preserves Qubes' Xen, qrexec,
QubesDB, qubesd, libvirt, policy, and VM lifecycle machinery.

The [personal application VM](docs/usr-home.md) follows the pinned NixOS GNOME
application defaults with GNOME Web and normal user conveniences. Other VMs
retain their existing minimal configuration and role-specific packages.

The deployable release outputs are:

```text
.#airlock-machine-release-usb-lab
.#airlock-machine-release-usb-gui-lab
```

They contain a host system, the complete declared VM topology, the exact setup
and coordinator programs, and this repository's revision and lock hash. They
use ordinary closure manifests for every guest and do not evaluate or build a
USB disk image or prepared guest-store payload.

All Nix commands must use the repository wrapper. It selects the isolated
store rooted at `/run/media/user/data/airlock/nix-root` and fails closed when
the external Airlock data location is unavailable. Routine development must
not opt into the host Nix store.

```bash
make test-quick
# Optional Nix evaluation and package checks:
make test-nix
make check-eval # flake evaluation only
make machine-build
```

USB image assembly, flashing, provisioning, development-host orchestration,
and cross-repository deployment tests live in the main `airlock` repository.
That repository pins this one as its `airlock-machine/` Git submodule and adapts the same machine definitions
to the prepared-store USB image format.

VM removal uses recoverable retirement before permanent deletion. Rollback can
retire a VM added by a trial release; a normal topology removal declares the
exact removed names in `airlock.machineDeployment.retireDomains`. Retirement
stops the VM and retains its private volume under its original Qubes identity.

Permanent purge requires every retained release and bootable host profile that
references the VM to have been safely pruned. The running host and immediate
rollback release remain protected. Apply another committed release that omits
the retired VM, then boot that release before pruning the trial and purging its
exact trash record. Reapplying the same release does not advance the rollback
choice. The typed prune and purge interfaces check these references and storage
ownership; confirmation or a retention-deadline override cannot bypass them.
