# Lifecycle source layout

- [image/](image/README.md): bootable image profiles and hardware configuration.
- [bootstrap/](bootstrap/README.md): initial VM declarations and setup.
- [runtime/](runtime/README.md): deployment, activation, inspection, and recovery.
- [misc/](misc/README.md): shared Nix modules, libraries, and tools.

The machine flake composes dom0 and VM systems. Image preparation supplies the
initial artifacts and source checkout; setup materializes the declared Qubes
topology. The installed checkout in usr-assistant is used to submit subsequent
committed configuration to sys-ctrl. Dom0 validates and activates releases.
See [machine configuration](../docs/config-system.md) and the
[workspace guide](../docs/usr-assistant.md).

`bootstrap/initial-runtime-config/` and the staged config-VM helpers are retained
prototype implementation. They do not describe the normal installed workspace.
