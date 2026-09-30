# Airlock Machine

`airlock-machine` is the independently buildable whole-machine source for
Airlock. It owns the whole-machine Nix configuration, declared dom0 and Qubes
VM systems, runtime services and packages, bootstrap configuration, and the
tests that maintain those components. It preserves Qubes' Xen, qrexec,
QubesDB, qubesd, libvirt, policy, and VM lifecycle machinery.

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
make check-eval
make test-quick
# Optional Nix evaluation and package checks:
make test-nix
make machine-build
```

USB image assembly, flashing, provisioning, development-host orchestration,
and cross-repository deployment tests live in the main `airlock` repository.
That repository pins this one as its `airlock-machine/` Git submodule and adapts the same machine definitions
to the prepared-store USB image format.
