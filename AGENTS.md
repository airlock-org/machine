# Airlock Agent Notes

- Preserve the Qubes architecture for the POC. Do not replace Xen, qrexec,
  QubesDB, qubesd, libvirt, or Qubes policy with new designs.
- Prefer upstream code, especially `CertainLach/nixos-qubes`, NixOS Xen support,
  and Qubes' own tests.
- Author first-party machine, package and image configuration in Rust/Rusix.
  Generated Nix remains the build interface to upstream NixOS and Qubes. Keep
  existing Qubes runtime state such as `qubes.xml` where its architecture requires it.
- From the external development computer, physical deployment orchestration
  lives in the parent `airlock` repository. Commit machine changes here first,
  then update and commit the `airlock-machine/` submodule pointer there.
  From the installed usr-assistant workspace, read `scripts/runtime/README.md` and use
  `airlock machine` directly from this clean committed checkout;
  a parent checkout is not required. A successful build is not a live deployment.
- Controller submissions return after the human review decision. An approved
  reply is not deployment success: follow the returned request ID with
  `airlock machine review-status --request-id REQUEST_ID`. Wait for the plan
  before apply and verify the deployment outcome before further system changes.
  Discuss rejection or cancellation instead of automatically resubmitting.
- Keep development observation separate from production behavior. Observation
  tools should report state without expanding Qubes control authority.
- Before editing deployable system configuration for a live target, inspect its
  recorded deployed source and read-only status/verification. Compare that source
  with the checkout, including committed and uncommitted changes; a clean Git
  tree does not prove deployment. Record the baseline and starting revision.
- If older deployable changes, an unfinished/failed current deployment or a
  required reboot remain, stop new work and report them. Ask the user whether to
  deploy, discard or isolate the older work; do not do so automatically. If the
  deployed baseline cannot be established, stop rather than assume a match.
  Documentation/instruction-only changes do not require deployment; inspect mixed
  commits by content. Explicitly requested work to resolve the backlog may proceed.
- Recheck the baseline, deployment state and checkout immediately before
  submission. Stop if they changed beyond the user's authorized change; do not
  bundle intervening work into its review. Never bypass source approval or build
  a candidate just to discover whether older changes are pending.
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
  Keep package selections in their Rust component; link there rather than maintain
  package inventories or installation/removal logs in docs. Git and deployment
  records provide the change history. Preserve dated validation evidence.
- Keep `test-quick` free of Nix evaluations, builds and automatic tool provisioning.
  `make test-nix` is opt-in; do not run it as a routine pre-deployment gate.
- Test Rust behavior with Cargo unit/integration tests and compile-fail doctests.
  Put process, filesystem and evaluation orchestration in Rust too. Keep Python
  fixtures only where upstream Qubes Python objects must be exercised.
  Avoid compiling ad-hoc Rust drivers when Cargo can exercise the same behavior.
  Do not add new Bash test scripts.
- Do not make full GUI support block the first qrexec/VM lifecycle milestone.
- Commit changes after each coherent conceptual change when practical. Use
  judgment about granularity, but avoid bundling unrelated work into one commit.
- Long Airlock image/template/VM iterations can grow the custom store quickly.
  Run `make gc-auto` periodically and after large failed build loops. Use only
  the guarded Airlock GC targets on the external development computer; do not
  garbage collect the host `/nix/store` or usr-assistant's pool-backed builder.

## Documentation

- Code structure, types, tests, and source comments document implementation.
  Explain non-obvious decisions and invariants beside the relevant code.
- Product introductions and user guides belong in the sibling website project.
  Do not duplicate them here or add forwarding stubs.
- Keep repository prose to root guidance, essential operating instructions, and
  source-local notes that cannot be understood from the implementation alone.
- Preserve investigation and validation evidence in Git history. Do not recreate
  status, TODO, completed-work, or historical-report document collections.

## Rust authoring

Follow the [parent Rust documentation and formatting standards](../AGENTS.md#rust-documentation-and-formatting).
Machine and candidate crates are internal and must remain `publish = false`.
Commit Rust declarations and locks. `make prepare` emits Nix in the ignored `target/nix/` directory;
do not add generated artifacts to Git.
Generation is separate from evaluation, building, source approval and deployment.
