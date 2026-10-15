# Component testing

`airlock-machine` owns the runtime component tests. Run them from this checkout:

```bash
make test-quick
make test-quick TESTS="host-capabilities topology-schema"
make test-quick PYTEST_ARGS="-k rollback"
make test-quick PYTEST_ARGS="-n 4 --dist loadgroup"
```

The suite collects `tests/`, including deployment and recovery contracts,
bootstrap/setup/apply helpers, storage, guest configuration and firmware.
`make test-quick` excludes Nix-marked cases; use `make test-nix` for package and
module evaluations or builds. `tests/test_machine_local.py` retains the named local
checks; their implementations live in `tests/machine_pychecks.py`. These checks
use this repository's source files and flake, without requiring a parent checkout.

Tests should check observable behavior and failure handling. Optional application
and development-tool selections belong in Nix; do not duplicate them as package
allowlists or denylists in tests. Avoid checking literal source text or repeating
successful configuration assertions when an existing configuration check already
covers them. For required dependencies, exercise the command or service that needs
them. Keep rejection, approval, isolation and recovery tests that verify what
happens before and after a failure.

Tests use the guarded external Airlock store and write reports under
`AIRLOCK_LOG_DIR`. The default store is
`/run/media/user/data/airlock/nix-root`; do not use the host store for routine
component development. Tests with physical or Qubes runtime requirements retain
their explicit opt-in environment variables.

For a subset without Nix-marked or Qubes-marked cases:

```bash
AIRLOCK_PYTEST_WARM_STORE=0 make test-quick PYTEST_ARGS="-m 'not nix and not qubes'"
```

The parent `airlock` repository runs these same component tests directly from
its pinned `airlock-machine/` submodule, alongside its own USB assembly and
transport tests. Nested-VM orchestration, `make test-warm`, `make test-slow`,
and the physical deployment workflow belong to that parent repository; see its
`docs/testing.md` for those commands. Component tests do not prove a successful
live deployment.
