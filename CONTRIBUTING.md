# Contributing to machine configuration

This repository owns deployable Rust/Rusix configuration and runtime integration.
Applications and their backends live in the parent airlock repository. Follow
[AGENTS.md](AGENTS.md) for repository and deployment rules.

## Checks and builds

The parent application checkout owns the shared Cargo regression suite. Prepare
its native executables explicitly, then run relevant checks:

```sh
make -C .. native-integration-build
make test-quick
```

Tests exercise behavior and failure handling. Quick tests do not provision tools
or evaluate Nix. Set `AIRLOCK_TEST_SOURCE` to an application checkout when this
repository is separate from its parent. Evaluations are opt-in with `make test-nix`.
Routine package-list edits need diff review and verification of deployed package
state; documentation edits need formatting and link review rather than tests.

On an external x86_64-linux development computer, choose the isolated store and
build a release through Make:

```sh
export AIRLOCK_DATA_ROOT=/path/to/airlock-data
make machine-build
```

Inside usr-assistant, use the installed Nix wrapper and pool-backed builder.
Do not apply external-development GC targets to that builder or collect the
shared pool or dom0 store. Reports use `AIRLOCK_LOG_DIR`.

## Changes

Commit each coherent change here. When working in the parent checkout, update
and commit its submodule pin afterwards. Installed-machine changes use the
[deployment workflow](scripts/runtime/README.md) without a parent checkout.
For external image/provisioning work, use the parent's
[image operations](https://github.com/airlock-org/airlock/blob/main/docs/image-operations.md).

Keep implementation explanations in source and tests. Product documentation
belongs to the website; Git history retains investigation and validation records.

Original contributions use the [MIT license](LICENSE). Preserve
[third-party terms](THIRD_PARTY.md) and follow [SECURITY.md](SECURITY.md) for
sensitive reports.
