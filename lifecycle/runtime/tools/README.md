# Runtime tools

- `airlock.py`: installed machine-management CLI.
- `machine-deploy.py` and `airlock_machine_deploy/`: dom0 deployment coordinator.
- `sys-ctrl-*.py`: committed-source submission, review, build, and typed control.
- `pkgs/`: Nix packaging for these tools and storage helpers.

Bootstrap-only and image-only tooling lives in the corresponding lifecycle
layers. Cross-stage helpers and development tools live in `../../misc/tools/`.
