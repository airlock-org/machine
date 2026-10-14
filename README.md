# Airlock Machine

Airlock Machine is the independently buildable whole-machine source for
[Airlock](https://github.com/airlock-org/airlock). It owns NixOS dom0 and guest
systems, bootstrap, runtime services/packages, and component tests. It preserves
Qubes' Xen, qrexec, QubesDB, qubesd, libvirt, policy, and VM lifecycle machinery.

Airlock is experimental. Physical evidence is specific to the current lab;
Qubes OS security equivalence and general hardware support are unproven. Image
assembly, provisioning, external development orchestration, and cross-repository
tests belong to the parent repository, which pins this one as a Git submodule.

## Local development

See [CONTRIBUTING.md](CONTRIBUTING.md) for prerequisites. Prepare dependencies
explicitly before running checks; no Xen host or application login is needed:

```sh
uv sync --locked
make test-quick
```

For Nix builds on x86_64-linux, choose an isolated development-store location
when the example lab mount is absent and use the repository Nix wrapper:

```sh
export AIRLOCK_DATA_ROOT=/path/to/airlock-data
# Optional Nix evaluations and package checks:
make test-nix
make machine-build
```

The whole-machine release outputs are:

```text
.#airlock-machine-release-usb-lab
.#airlock-machine-release-usb-gui-lab
```

They contain the host, declared VM topology, setup/coordinator programs, source
revision, and lock hash. They do not assemble a USB disk image. The default
hardware module provides the lab configuration; another machine can replace
`hardwareModules` in `lib.mkMachineConfiguration` and add `extraModules`.

## Runtime and evidence

The personal VM [usr-home](docs/usr-home.md) owns normal applications and files.
[usr-assistant](docs/usr-assistant.md) is the development/management workspace, and
sys-ctrl prepares committed deployment candidates. Tailscale enrollment remains
interactive, with persistent private state. Codex remains installed as an
application; credential importing and vault management are removed.

Use the parent `docs/README.md` and `docs/status.md` for current lab runbooks and
acceptance boundaries. Older component notes describe their named profiles and
are not independent authorities for the physical lab. Updating source locally
is not live deployment verification.

VM removal uses recoverable retirement before permanent deletion. Purge requires
safe pruning of retained releases and bootable profiles referencing that VM;
the running host and immediate rollback release stay protected. Rollback does
not promise reversal of application data migrations.

Original Airlock code is [MIT licensed](LICENSE); see [THIRD_PARTY.md](THIRD_PARTY.md)
for upstream terms and the outstanding vendored-source licensing question.
See [SECURITY.md](SECURITY.md) for reporting sensitive issues.
