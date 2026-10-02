# Working on Airlock from sys-agent

These instructions describe the installed sys-agent development workspace.
Honor discussion-only requests. Complete authorized work without repeatedly
asking for permission; apply a deployment when the user's request authorizes it.

## Find the system and source

- `sys-agent` is the connected development and management VM. Its Console,
  Codex, checkout, and private data live here; sys-gui displays its windows.
- `sys-gui` owns GNOME and forwards application windows. `usr-home` owns the
  user's applications and files. Both usr-home and sys-agent connect through
  `sys-firewall`; `sys-net` owns the network device.
- `sys-ctrl` builds committed sources and submits validated artifacts. Dom0
  owns Xen, Qubes policy, VM configuration, and journalled deployment.
- The editable machine checkout is `~/airlock-machine`. Start
  with `git status`, `AGENTS.md`, `README.md`, and `docs/sys-agent.md` there.
- Guest modules are in `lifecycle/misc/modules/`; built-in VM defaults are in
  `lifecycle/bootstrap/default-domains/`. `flake.nix` exposes configurations
  and releases. Deployment clients are in `lifecycle/runtime/tools/`.
- Preserve Xen, qrexec, QubesDB, qubesd, libvirt, and Qubes policy. Prefer the
  upstream integrations and keep Nix as the source of truth.
- Commit successful fixes in the checkout; temporary live edits, `/run`
  changes, or overlays do not constitute a deployed fix.

## Edit, check, and commit

Work in the checkout. Keep unrelated user changes intact. The seeded checkout
can have a detached HEAD; create a named branch before new work. If Git has no
author identity, ask the user for their name/email before making commits.
The seeded remote can point at the external development computer; a working
upstream remote is not required for local commits or deployment.

Run `make test-quick`. The sys-agent login environment sets `AIRLOCK_LOG_DIR`
to `/home/user/.local/state/airlock/logs`; keep the test reports and logs there.
The quick suite must remain free of Nix evaluations/builds and automatic tool
provisioning. `uv sync --locked` prepares the pinned Python dependencies if
needed before testing. Add meaningful regressions in pytest. Run focused
`make test-nix` checks only when the change needs evaluation or packaged checks.
Commit each coherent change, including `flake.lock` when deliberately changed.
Deployment requires a clean committed checkout.

## Deploy through the existing controller

For whole-machine changes, including VM additions, routing, preferences, or
policy, run these commands from sys-agent:

```sh
airlock-sys-agent-machine-deploy plan
airlock-sys-agent-machine-deploy apply
airlock-sys-agent-machine-deploy status
airlock-sys-agent-machine-deploy verify
```

`plan` builds and validates without activation. Inspect its changed/affected
VMs, stop/start list, retirement actions, and reboot requirement before apply.
`apply` submits a detached dom0 worker. Submission or a successful build alone
does not establish deployment success: wait for the worker outcome and verify
the selected/running generations for the host and every VM.

For a guest-package/module change with no topology or host-policy change:

```sh
airlock-sys-agent-machine-deploy guest-plan --target usr-home
airlock-sys-agent-machine-deploy guest-apply --target usr-home
airlock-sys-agent-machine-deploy status
airlock-sys-agent-machine-deploy verify
```

Replace `usr-home` with the changed guest. A sys-agent update terminates this
Console/Codex process; the worker continues. Reconnect, inspect status, and
resume the saved Codex session. Zellij survives a terminal disconnection, not
a VM restart. A selected host update may require the user to reboot the laptop;
report guest verification separately and verify again after reboot.

## Observe and recover

- `airlock-sys-agent-machine-deploy status` reads the journal even when the
  controller is unavailable; `verify` checks running generations.
- `~/.local/share/airlock/machine-deploy/` contains agent deployment state.
  The controller keeps source candidates and observations under
  `~/.local/share/airlock/controller/` inside sys-ctrl.
- If an interrupted transaction needs continuation, inspect status first,
  then use `airlock-sys-agent-machine-deploy resume` when appropriate.
  `airlock-sys-agent-machine-deploy rollback` restores the previous selection;
  assess its restart effects and the user's authorized scope first.
- The development-only `airlock-dom0-shell` provides diagnostics if needed.
  Use read-only observation first; normal deployment uses typed controller
  commands. Record and remove unavoidable diagnostic mutations before verify.
- The installed `nix` wrapper selects the approved pool-backed private builder
  at `/rw/airlock-builder/merged`. Do not bypass it or garbage-collect the
  shared pool, dom0's store, or the builder's approved base. Repository GC
  targets are for the external development store, not this guest builder.

Keep authentication in the private Codex directory. Never put credentials in
Nix configuration, Git, logs, or deployment archives.
