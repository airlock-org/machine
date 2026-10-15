# Component testing

`airlock-machine` owns the runtime component tests.

[CONTRIBUTING.md](../CONTRIBUTING.md#local-checks) describes which checks apply
to a change and how to prepare dependencies. This guide covers test selection,
coverage, and reporting.

Run tests from this checkout:

```bash
make test-quick
make test-quick TESTS=setup-config-idempotent
make test-quick PYTEST_ARGS="-k rollback"
make test-quick PYTEST_ARGS="-n 4 --dist loadgroup"
```

The suite collects `tests/`, including deployment and recovery contracts,
bootstrap/setup/apply helpers, storage, guest configuration and firmware.
`make test-quick` excludes Nix-marked cases; use `make test-nix` for package and
module evaluations or builds. `tests/test_machine_local.py` retains the named
local checks; their implementations live under `tests/local_checks/` and use
this repository's source files and flake, without requiring a parent checkout.

Nix-marked checks are selected explicitly, for example:

```bash
make test-nix TESTS=topology-schema
```

Tests should check observable behavior and failure handling. Optional application
and development-tool selections belong in Nix; do not duplicate them as package
allowlists or denylists in tests. Avoid checking literal source text or repeating
successful configuration assertions when an existing configuration check already
covers them. For required dependencies, exercise the command or service that needs
them. Keep rejection, approval, isolation and recovery tests that verify what
happens before and after a failure.

The shared `tests/airlock_test_support` plugin owns session setup and reporting.
The parent imports that same plugin, while each checkout keeps its own
`repo_root` fixture. A combined run writes one session record.

Protocol unit tests compile the C implementation carried by our GUI patches and
exercise it with inert X11/vchan boundaries. Coordinator/setup tests execute the
real stage and setup paths against simulated Qubes commands. These checks do not
claim to prove that the full GUI stack or a live VM boots.

Storage coverage has a separate CI job that explicitly prepares patched EROFS
tools. Run its required tool checks locally from a clean committed checkout:

```bash
./lifecycle/misc/tools/scripts/airlock-nix.sh shell .#airlock-erofs-utils --command \
  uv run --no-sync pytest tests/test_incremental_store_index.py \
  tests/test_pool_artifact.py tests/test_pool_maintenance.py \
  -m 'not nix' --require-erofs
```

`--require-erofs` fails before tests run if the tools or Airlock tar-offset
support are missing. Ordinary quick runs retain optional skips for these tools;
they never build or provision them.

Reports are written under `AIRLOCK_LOG_DIR`. For Nix checks on an external
development computer, set `AIRLOCK_DATA_ROOT` for the isolated store; the existing
`/run/media/user/data/airlock/nix-root` path is a development mount default.
Inside usr-assistant, use the installed builder wrapper described in the
[workspace guide](usr-assistant.md#stores-and-source-locations).
Tests with physical or Qubes runtime requirements retain their explicit opt-in
environment variables.

For a subset without Nix-marked or Qubes-marked cases:

```bash
AIRLOCK_PYTEST_WARM_STORE=0 make test-quick PYTEST_ARGS="-m 'not nix and not qubes'"
```

The parent `airlock` repository runs these same component tests directly from
its pinned `airlock-machine/` submodule, alongside its image assembly and
transport tests. Nested-VM orchestration, `make test-warm`, `make test-slow`,
and the physical deployment workflow belong to that parent repository; see its
[testing guide](https://github.com/airlock-org/airlock/blob/main/docs/testing.md)
for those commands. Component tests do not prove a successful
live deployment.
