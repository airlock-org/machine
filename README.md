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
the Rust configuration for dom0 and the other VMs, commit changes, and submit
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

[Deployment and recovery commands](scripts/runtime/README.md) cover
source approval, planning, activation, and verification. Installed-machine
changes do not require a parent checkout or a push to GitHub.

## Authoring

Start with [the physical machine](src/physical.rs). Each VM is declared,
configured through its typed handle and registered with the machine. Shared
system composition and runtime contracts come from the pinned `airlock-config`
crate. [The nested machine](src/bootstrap.rs) supplies the validation environment.

Commit Rust source and dependency locks. `make prepare` inside an installed
authoring VM emits a content-addressed build tree under ignored `target/nix/`
and prints its `path:` flake reference. The admitted input mount supplies the authoring library and
offline Cargo dependencies. Workstations can override those paths with
`AIRLOCK_APPS` and `AIRLOCK_CARGO_VENDOR`. Generated Nix remains inspectable in
the prepared artifact, outside Git. Preparation does not build or deploy a machine.

## Contributing

Supporting files are grouped by purpose: `assets/` holds component files,
`bootstrap/` holds the initial editable runtime configuration, `scripts/` holds
installed-machine operating guidance and the workstation environment adapter,
and upstream source remains in `vendor/`. Shared integration patches and desktop
implementation assets come from the pinned Airlock application source.
[CONTRIBUTING.md](CONTRIBUTING.md)
covers developer setup, checks, and release builds.

The parent [airlock repository](https://github.com/airlock-org/airlock) pins this
repository as a Git submodule and handles image assembly, provisioning, external
deployment tooling, and integration tests. For deployment from an external
computer, use its [development operations](https://github.com/airlock-org/airlock/blob/main/docs/development-operations.md).

AirlockOS is under active development. Qubes OS security equivalence has not
been established. See [SECURITY.md](SECURITY.md) for limitations and reporting.

Original code is [MIT licensed](LICENSE). Dependencies retain their own terms;
see [third-party notices](THIRD_PARTY.md).
