# Airlock Agent Notes

- Preserve the Qubes architecture for the POC. Do not replace Xen, qrexec,
  QubesDB, qubesd, libvirt, or Qubes policy with new designs.
- Prefer upstream code, especially `CertainLach/nixos-qubes`, NixOS Xen support,
  and Qubes' own tests.
- Keep Nix as the source of truth wherever practical, but accept existing Qubes
  runtime state such as `qubes.xml` for the first milestone.
- Physical deployment orchestration lives in the parent `airlock`
  repository. Commit machine changes here first, then update and commit the `airlock-machine/`
  submodule pointer there. A successful build is not a live deployment.
- Keep development observation separate from production behavior. Observation
  tools should report state without expanding Qubes control authority.
- `make test-quick` is the primary component feedback loop. Preserve clear logs
  and machine-readable output under the external Airlock log directory.
- Keep `test-quick` free of Nix evaluations, builds and automatic tool provisioning.
  `make test-nix` is opt-in; do not run it as a routine pre-deployment gate.
- New tests should be written in the pytest framework. Do not add new Bash test
  scripts; migrate or extend pytest coverage instead.
- Do not make full GUI support block the first qrexec/VM lifecycle milestone.
- Commit changes after each coherent conceptual change when practical. Use
  judgment about granularity, but avoid bundling unrelated work into one commit.
- Long Airlock image/template/VM iterations can grow the custom store quickly.
  Run `make gc-auto` periodically and after large failed build loops. Use only
  the guarded Airlock GC targets; do not garbage collect the host `/nix/store`.
