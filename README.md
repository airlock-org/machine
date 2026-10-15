<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/airlock-wordmark-thin-red-dark.svg">
    <img alt="AirlockOS" src="docs/assets/airlock-wordmark-thin-red-light.svg" height="64">
  </picture>
</p>
<p align="center">Machine configuration</p>

This repository is the machine configuration workspace for
[AirlockOS](https://github.com/airlock-org/airlock). On an installed system, it
lives at `~/airlock-machine` inside `usr-assistant`. Use it to review and edit
the Nix configuration for dom0 and the other VMs, commit changes, and submit
deployments.

`usr-assistant` is the system workspace for configuration and administration.
Personal applications and files remain in `usr-home`.

## Using the workspace

Open **usr-assistant: Console** from GNOME, then inspect the running machine:

```sh
cd ~/airlock-machine
airlock machine status
airlock machine verify
```

Use `airlock --help` to list command groups and `airlock machine --help` for
machine actions and options.

Follow the [workspace guide](docs/usr-assistant.md) to compare the checkout with
the deployed source, edit and commit configuration, review the deployment plan,
and apply and verify changes. It also covers source approval and recovery.
Installed-machine changes do not require a parent repository checkout or a push
to GitHub.

## Configuration files

- [flake.nix](flake.nix): machine configurations and release outputs.
- [lifecycle/misc/modules/](lifecycle/misc/modules/): NixOS modules for VM software and services.
- [lifecycle/bootstrap/default-domains/](lifecycle/bootstrap/default-domains/): default VM topology and settings.

See the [configuration guide](https://github.com/airlock-org/airlock/blob/main/docs/lab-configuration.md)
for module composition and hardware settings.

## Contributing

This repository also contains bootstrap and deployment tooling, runtime
services and packages, and component tests. [CONTRIBUTING.md](CONTRIBUTING.md)
covers developer setup, checks, and release builds.

The parent [airlock repository](https://github.com/airlock-org/airlock) pins this
repository as a Git submodule and handles image assembly, provisioning, external
deployment tooling, and integration tests. For deployment from an external
computer, use its [deployment guide](https://github.com/airlock-org/airlock/blob/main/docs/machine-deployment.md).

AirlockOS is under active development. Qubes OS security equivalence has not
been established. See [SECURITY.md](SECURITY.md) for limitations and reporting.

Original code is [MIT licensed](LICENSE). Dependencies retain their own terms;
see [third-party notices](THIRD_PARTY.md).
