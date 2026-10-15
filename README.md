<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/airlock-wordmark-thin-red-dark.svg">
    <img alt="AirlockOS" src="docs/assets/airlock-wordmark-thin-red-light.svg" height="64">
  </picture>
</p>
<p align="center">Machine configuration</p>

This repository defines the NixOS configuration for [AirlockOS](https://github.com/airlock-org/airlock):
dom0, system VMs, and user VMs. It contains the VM topology, bootstrap,
runtime services and packages, and component tests, using the Qubes architecture.

The parent [airlock repository](https://github.com/airlock-org/airlock) handles
image assembly, provisioning, deployment from an external computer, and
integration tests. It pins this repository as a Git submodule.

## Build and checks

See [CONTRIBUTING.md](CONTRIBUTING.md) for prerequisites. Prepare dependencies
and run the quick checks:

```sh
uv sync --locked
make test-quick
```

For Nix builds on x86_64-linux, choose an isolated development-store location:

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

They contain the dom0 system, VM topology, setup and deployment programs, source
revision, and lock hash. USB disk image assembly lives in the parent repository.

## Documentation

- [Personal applications and files in usr-home](docs/usr-home.md).
- [System workspace in usr-assistant](docs/usr-assistant.md): configuration, administration, and deployment from the installed machine.
- [Component testing](docs/testing.md).
- [Machine configuration](https://github.com/airlock-org/airlock/blob/main/docs/lab-configuration.md): configuration modules and hardware settings.
- [Deployment and rollback](https://github.com/airlock-org/airlock/blob/main/docs/machine-deployment.md): deployment from an external computer and recovery procedures.

AirlockOS is under active development. Qubes OS security equivalence has not
been established. See [SECURITY.md](SECURITY.md) for limitations and reporting.

Original code is [MIT licensed](LICENSE). Dependencies retain their own terms;
see [third-party notices](THIRD_PARTY.md).
