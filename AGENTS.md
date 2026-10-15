# Airlock Agent Notes

- Preserve the Qubes architecture for the POC. Do not replace Xen, qrexec,
  QubesDB, qubesd, libvirt, or Qubes policy with new designs.
- Prefer upstream code, especially `CertainLach/nixos-qubes`, NixOS Xen support,
  and Qubes' own tests.
- Keep Nix as the source of truth wherever practical, but accept existing Qubes
  runtime state such as `qubes.xml` for the first milestone.
- From the external development computer, physical deployment orchestration
  lives in the parent `airlock` repository. Commit machine changes here first,
  then update and commit the `airlock-machine/` submodule pointer there.
  From the installed usr-assistant workspace, read `docs/usr-assistant.md` and use
  `airlock-usr-assistant-machine-deploy` directly from this clean committed checkout;
  a parent checkout is not required. A successful build is not a live deployment.
- Keep development observation separate from production behavior. Observation
  tools should report state without expanding Qubes control authority.
- Match verification to the change. For routine package-list additions/removals,
  skip repository tests: review and commit the exact diff, obtain required source
  approval before evaluation/build, then apply and verify the requested package
  state. Keep normal Nix build checks enabled.
- For implementation, service, policy or deployment changes, run relevant tests;
  use `make test-quick` when shared behavior could be affected. For documentation
  changes, review the diff and check formatting; executable tests are unnecessary.
  Preserve clear logs and machine-readable output under the Airlock log directory.
- Do not add tests that merely repeat the configured package list.
- Routine package-list additions/removals do not require documentation updates
  unless they change documented behavior, instructions or supported capabilities.
  Keep package selections in Nix; link to that configuration rather than maintain
  package inventories or installation/removal logs in docs. Git and deployment
  records provide the change history. Preserve dated validation evidence.
- Keep `test-quick` free of Nix evaluations, builds and automatic tool provisioning.
  `make test-nix` is opt-in; do not run it as a routine pre-deployment gate.
- New tests should be written in the pytest framework. Do not add new Bash test
  scripts; migrate or extend pytest coverage instead.
- Do not make full GUI support block the first qrexec/VM lifecycle milestone.
- Commit changes after each coherent conceptual change when practical. Use
  judgment about granularity, but avoid bundling unrelated work into one commit.
- Long Airlock image/template/VM iterations can grow the custom store quickly.
  Run `make gc-auto` periodically and after large failed build loops. Use only
  the guarded Airlock GC targets on the external development computer; do not
  garbage collect the host `/nix/store` or usr-assistant's pool-backed builder.
