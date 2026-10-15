# Runtime

`tools/airlock.py` implements the installed `airlock machine` CLI.
`tools/airlock_machine_deploy/` implements the dom0 coordinator; controller
clients, artifact handling, storage, and recovery helpers live alongside it.
See the [coordinator guide](tools/airlock_machine_deploy/README.md) and
[workspace guide](../../docs/usr-assistant.md).

Shared Nix modules, schemas, and helpers live under `../misc/`.
