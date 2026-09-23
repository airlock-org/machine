# Machine deployment implementation

`../machine-deploy.py` remains the command-line entry point. Its Nix package
includes this directory alongside the entry point; importing it must not depend
on the checkout or a development `PYTHONPATH`.

- `cli.py` parses commands and formats responses and errors.
- `coordinator.py` sequences staging, deployment, recovery, and boot activation.
- `planning.py` computes lifecycle plans from topology data without performing I/O.
- `validation.py` checks release, admission, and storage ownership data without I/O.
- `retirement.py` handles VM retirement, restoration, and resumable trash purging.
- `releases.py` handles release inventory, retention, and resumable pruning.
- `state.py` loads releases and performs durable JSON writes.
- `models.py` describes plan and journal dictionaries without changing their JSON.

The coordinator supplies command execution, runtime inventory, and the shared
machine lock. Public purge and pruning operations acquire that same lock.
Retirement helpers called during deployment or boot use the caller's existing
lock, so they must not acquire a second lock. Keep journal writes and mutations
in order: interrupted operations are resumed from those records.

The existing CLI, journal fields, schema identifiers, and recovery behavior are
preserved. Tests in `tests/test_machine_deploy.py` exercise those contracts,
including interruption, rollback, shared locking, and installed-package imports.
