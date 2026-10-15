# Contributing to AirlockOS machine configuration

This guide covers changes to AirlockOS services, packages, bootstrap and
deployment tooling, and component tests. To configure an installed machine,
start with the [workspace guide](docs/usr-assistant.md).

Read [AGENTS.md](AGENTS.md) for repository rules. Preserve the existing
Qubes/Xen architecture and prefer upstreamable changes. Image assembly and
external deployment orchestration live in the parent Airlock repository.

## Local checks

On Linux, prepare Git, Bash, Make, Python >=3.11, uv, and ordinary coreutils and
OpenSSH tools. CI additionally installs jq, util-linux, e2fsprogs, and ripgrep.
Install dependencies explicitly:

```sh
uv sync --locked
```

Run relevant tests for implementation, service, policy, and deployment changes.
Use the quick suite when shared behavior could be affected:

```sh
make test-quick
```

The quick suite uses `uv run --no-sync`; it does not provision tools, evaluate
Nix, boot Xen, or require application enrollment. Reports use `AIRLOCK_LOG_DIR`;
without a configured data root or the existing development data mount, they
fall back to `.airlock-logs/`. New regression tests use pytest and verify
behavior and failure handling. See [component testing](docs/testing.md) for
targeted checks and reporting.

Routine package-list edits need diff review and verification of the deployed
package state rather than repository tests. Documentation changes need diff,
link, and formatting review. Keep normal Nix build checks enabled for system
changes.

## Nix builds

On an external x86_64-linux development computer, choose an isolated
storage location and use the repository's Nix wrapper through Make:

```sh
export AIRLOCK_DATA_ROOT=/path/to/airlock-data
# Optional Nix evaluations and package checks:
make test-nix
make machine-build
# Build the GUI release instead:
make machine-build-gui
```

The whole-machine release outputs are:

```text
.#airlock-machine-release-usb-lab
.#airlock-machine-release-usb-gui-lab
```

They contain the dom0 system, VM topology, setup and deployment programs, source
revision, and lock hash. USB disk image assembly lives in the parent repository.

The default outputs use the existing development hardware configuration.
`lib.mkMachineConfiguration` accepts a replacement `hardwareModules` list and
additional `extraModules`. See the
[parent configuration guide](https://github.com/airlock-org/airlock/blob/main/docs/lab-configuration.md).

Inside usr-assistant, the installed Nix command uses the machine's pool-backed
builder. Follow the [workspace store guidance](docs/usr-assistant.md#stores-and-source-locations)
for that environment.

## Commits and deployment

Commit each coherent change in this repository. When working in a parent
`airlock` checkout, update and commit its submodule pointer after committing
the machine change. Keep one editable source for each runtime helper and test.

For installed-machine changes, follow the
[workspace deployment procedure](docs/usr-assistant.md#change-the-running-machine),
including the deployed-source baseline check and normal source approval.
No parent checkout is required. For deployment from an external computer, use
the [parent deployment guide](https://github.com/airlock-org/airlock/blob/main/docs/machine-deployment.md).
A successful build does not verify live deployment. Record and remove diagnostic
mutations before verifying the resulting system generation.

Use guarded Airlock GC targets on the external development computer. Do not
collect its `/nix/store` or usr-assistant's pool-backed builder.

## Updating Codex

The upstream Codex release is pinned in
[lifecycle/misc/overlays/codex-release.nix](lifecycle/misc/overlays/codex-release.nix),
with the version and archive hash in
[lifecycle/runtime/tools/pkgs/codex-release/default.nix](lifecycle/runtime/tools/pkgs/codex-release/default.nix).
On x86_64 Linux, its CLI, code-mode host, sandbox tools, and other runtimes are
installed together; glibc-linked runtime helpers are adapted to Nix during the
build. Commit, deploy, and verify `codex --version` in the running usr-assistant.
An npm or home-directory replacement does not update the managed package.

Original contributions use the MIT license in [LICENSE](LICENSE). Preserve
third-party terms and attribution in [THIRD_PARTY.md](THIRD_PARTY.md); follow
[SECURITY.md](SECURITY.md) for sensitive reports.
