# Develop and deploy from sys-agent

Search GNOME for **sys-agent: Console**. Console is the ordinary GNOME Console
application running inside sys-agent, with its windows forwarded through Qubes
to sys-gui. It is discovered from the VM's own desktop entries, like apps in
usr-home. Open it and start Codex in the persistent machine checkout:

```sh
cd ~/airlock-machine
codex
```

Codex, Console, Git, ripgrep, Make, Python, uv, and Zellij are installed through
Nix. The checkout, login credentials, session history, and Python dependency
cache live on sys-agent's private volume and survive root-generation updates.
The existing upstream remote is not needed to commit and deploy locally.

Sys-agent uses PVH with no assigned PCI devices. Sys-net owns the physical
network hardware; sys-agent reaches the internet through sys-firewall.

## First use

Codex needs your own login. For a ChatGPT account with device login enabled,
run `codex login --device-auth` in sys-agent, then complete the displayed code
using GNOME Web in usr-home. API-key login is also available. Credentials remain
inside sys-agent; the system does not import another VM's browser profile or
embed authentication in the machine configuration.

`~/AGENTS.md` is a brief introduction pointing to `~/airlock-machine` and the
full installed guidance. On upgrade, the previous `~/dev/airlock-machine`
checkout moves as a whole, preserving local branches and uncommitted files.
An existing destination or user-written home introduction is preserved.
If you already had a `.venv` in the moved checkout, run
`uv sync --locked --reinstall` once to refresh launchers containing the old path.

`CODEX_HOME` points at `~/.codex`. On first boot, a symlink there named
`AGENTS.md` points at `/etc/airlock/sys-agent/AGENTS.md`, which contains the VM
map, source locations, tests, deployment, observation, and recovery commands.
It follows the selected system generation. An existing user-written file or
override is preserved. The checkout's own `AGENTS.md` supplies project rules.
Official setup references: [authentication](https://developers.openai.com/codex/auth/)
and [instruction discovery](https://developers.openai.com/codex/guides/agents-md/).

Before your first commit, configure your own Git name and email if they are
missing. Create a branch when the seeded checkout is detached. Prepare pinned
Python dependencies with `uv sync --locked`, then run `make test-quick`.
Tests use the installed Python, with automatic Python downloads disabled.
Reports go to `$AIRLOCK_LOG_DIR` (`~/.local/state/airlock/logs`).

## Change the running machine

Edit and test this checkout, commit the change, then inspect the normal plan:

```sh
airlock-sys-agent-machine-deploy plan
airlock-sys-agent-machine-deploy apply
airlock-sys-agent-machine-deploy status
airlock-sys-agent-machine-deploy verify
```

These commands send locked committed source to sys-ctrl. The controller builds
and admits validated artifacts through the existing Qubes interfaces; dom0
coordinates activation. No parent `airlock` checkout is needed for installed
machine changes. The external development computer still uses its parent repo
and submodule workflow for image and development-tool changes.

The single-guest `guest-plan --target VM` / `guest-apply --target VM` route is
for guest-only changes. Use the whole-machine route for topology, new VMs,
routing, preferences, and host policy. Read the plan before activation.
Status and verification are required after apply. If the host requires a
reboot, verify again after the laptop restarts.

Updating sys-agent closes its Console and Codex processes. Deployment continues
in a detached dom0 worker; reconnect and use `codex resume` after checking the
worker outcome. Zellij can preserve an interactive session across terminal/SSH
disconnects, but cannot keep a process alive across a VM restart.

For recovery, inspect `status` before using `resume` or `rollback`. A failed
controller does not prevent the typed status/verification/recovery interface.
The existing `airlock-dom0-shell` remains available for lab diagnostics.
Fix the committed source rather than leaving runtime configuration overlays.

## Stores and source locations

The installed `nix` command selects the approved pool-backed builder store at
`/rw/airlock-builder/merged`; read-only shared inputs remain pinned independently
by dom0. Workspace state and logs live under `~/.local/state/airlock`.
Do not run the external-development GC targets against the guest builder or
collect the shared pool or dom0 store.

The main files are `flake.nix`, guest modules in `lifecycle/misc/modules/`, VM
defaults in `lifecycle/bootstrap/default-domains/`, and deployment clients in
`lifecycle/runtime/tools/`. See [the builder design](config-system.md) and the
parent repository's `docs/machine-deployment.md` when working externally.
