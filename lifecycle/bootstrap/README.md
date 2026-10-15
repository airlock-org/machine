# Bootstrap

Bootstrap modules define initial Qubes topology, VM defaults, and setup behavior.
[default-domains/](default-domains/) supplies the system and user VM declarations.
The machine flake and image profiles select which declarations to materialize.

The installed usr-assistant checkout is seeded by
[machine-source-seed.nix](../misc/modules/machine-source-seed.nix), which preserves
an existing checkout. Runtime updates use sys-ctrl and the dom0 deployment
coordinator; see [machine configuration](../../docs/config-system.md).

`initial-runtime-config/` and the configuration-vault helpers belong to the
earlier staged configuration prototype. Supplied bootstrap profiles disable
that prototype's control policy. Bootstrap-only tools live in `tools/`; shared
helpers live in `../misc/tools/`.
