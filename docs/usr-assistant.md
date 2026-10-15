# System workspace in usr-assistant

`usr-assistant` is a separate VM for machine configuration and administration.
Its persistent checkout at `~/airlock-machine` is the configuration workspace
for the installed machine. Everyday applications and personal files remain in
`usr-home`.

System changes use the existing Qubes mechanisms. Sys-ctrl validates and builds
committed source; dom0 retains policy, lifecycle, storage, and activation
authority. Development-specific dom0 access is explicitly configured.

## Open the workspace

Search GNOME for **usr-assistant: Console**. Console runs in usr-assistant, with
its windows forwarded through Qubes to sys-gui, like applications in usr-home.
Open the checkout:

```sh
cd ~/airlock-machine
git status
```

Workspace software is declared in the
[VM module](../lifecycle/misc/modules/usr-assistant-guest.nix) and its imports.
The checkout, login credentials, session history, and Python dependency cache
live on usr-assistant's private volume and survive system-generation updates.
Committing and deploying locally does not require pushing to the upstream remote.

Usr-assistant uses PVH with no assigned PCI devices. Sys-net owns the physical
network hardware; usr-assistant reaches the internet through sys-firewall.

## Git identity and checks

Setup fills missing Git name and email values with `Airlock User` and
`user@airlock.invalid` in `~/airlock-machine` only. It runs after source seeding
and on subsequent boots, preserving existing local or global identity settings.
To use a personal identity instead:

```sh
git -C ~/airlock-machine config --local user.name 'Your Name'
git -C ~/airlock-machine config --local user.email 'you@example.com'
```

Create a branch when the seeded checkout is detached. Match verification to the
change: routine package-list additions/removals skip repository tests; review
and commit the exact diff, obtain required source approval before evaluation/build,
then apply and verify the requested package state. Keep normal Nix build checks
enabled. Changes to implementation, services, policy or deployment need relevant
tests; use `make test-quick` when shared behavior could be affected. Do not add
tests that merely repeat the configured package list. Documentation changes need
diff and formatting review rather than executable tests.

Routine package-list edits do not require documentation updates unless they
change documented behavior, instructions or supported capabilities. Keep package
selections in Nix and link to the configuration instead of maintaining inventories
or installation/removal logs. Git and deployment records provide the history.

When testing is needed, prepare pinned Python dependencies with `uv sync --locked`.
Tests use the installed Python, with automatic Python downloads disabled. Reports
go to `$AIRLOCK_LOG_DIR` (`~/.local/state/airlock/logs`).

## Change the running machine

Before editing deployable system configuration, inspect the target using the
read-only commands:

```sh
airlock machine status
airlock machine verify
```

Establish the recorded deployed-source baseline and compare its tree with the
checkout, including committed and uncommitted changes. Record that identity and
the starting checkout revision. A clean Git tree or successful build does not
prove deployment. Check dom0 and the other VMs: a single-VM update does not
establish that the whole checkout is active, and an uncompleted activation or
required reboot remains pending.

If older deployable changes, an unfinished/failed current deployment or an unknown
baseline remain, stop new system edits. Report the pending commits/files and
deployment state, then let the user choose whether to deploy, discard or isolate
the older work. Do not resolve it automatically or combine it with the new request.
Explicitly requested backlog resolution may proceed. Documentation/instruction-only
changes do not require deployment; inspect mixed commits by content.

Repeat the check immediately before submission. If the baseline, deployment state
or checkout changed beyond the recorded starting state and the user's authorized
change, stop and report it. Use read-only state and source comparisons for these
checks, without candidate evaluation/build or bypassing approval.

Once the check is clear, edit this checkout, verify according to the change,
commit, recheck, then inspect the normal plan:

```sh
airlock machine plan
airlock machine apply
airlock machine status
airlock machine verify
```

These commands send locked committed source to sys-ctrl. Normal submissions
require [source approval](controller-review.md) before evaluation and building.
The controller builds and admits validated artifacts through the existing Qubes
interfaces; dom0 coordinates activation. No parent `airlock` checkout is needed
for installed-machine changes. The external development computer still uses its
parent repo and submodule workflow for image and development-tool changes.

The `guest-plan --target VM` / `guest-apply --target VM` route is
for changes limited to an existing VM. Use the whole-machine route for topology,
new VMs, routing, preferences, and dom0 policy. Read the plan before activation.
Status and verification are required after apply. If dom0 requires a reboot,
verify again after the machine restarts.

Updating usr-assistant closes its running applications. Deployment continues in
a detached dom0 worker. Reopen Console after the VM restarts and check the worker
outcome; if using Codex, continue with `codex resume`. Zellij can preserve an
interactive session across terminal/SSH disconnects, but cannot keep a process
alive across a VM restart.

For recovery, inspect `status` before using `resume` or `rollback`. A failed
controller does not prevent the typed status/verification/recovery interface.
The existing `airlock-dom0-shell` remains available for lab diagnostics.
Fix the committed source rather than leaving runtime configuration overlays.

## Stores and source locations

The installed `nix` command selects the approved pool-backed builder store at
`/rw/airlock-builder/merged`; read-only shared inputs remain pinned independently
by dom0. Workspace state and logs live under `~/.local/state/airlock`.
Do not run the external-development GC targets against the usr-assistant builder
or collect the shared pool or dom0 store.

The main files are `flake.nix`, VM modules in `lifecycle/misc/modules/`, VM
defaults in `lifecycle/bootstrap/default-domains/`, and deployment clients in
`lifecycle/runtime/tools/`. See [the builder design](config-system.md) and the
[external deployment guide](https://github.com/airlock-org/airlock/blob/main/docs/machine-deployment.md)
when working from another computer.

## Optional: using Codex

Codex is available in Console. Start it from the configuration checkout:

```sh
cd ~/airlock-machine
codex
```

Codex needs your own login. For a ChatGPT account with device login enabled,
run `codex login --device-auth` in usr-assistant, then complete the displayed code
using a browser in usr-home. API-key login is also available. Credentials remain
inside usr-assistant; the system does not import another VM's browser profile or
embed authentication in the machine configuration.

`CODEX_HOME` points at `~/.codex`. On first boot, a symlink there named
`AGENTS.md` points at `/etc/airlock/usr-assistant/AGENTS.md`, which contains the VM
map, source locations, tests, deployment, observation, and recovery commands.
It follows the selected system generation. An existing user-written file or
override is preserved. The checkout's own `AGENTS.md` supplies project rules.
Official setup references: [authentication](https://developers.openai.com/codex/auth/)
and [instruction discovery](https://developers.openai.com/codex/guides/agents-md/).

## Workspace instructions and older checkouts

`~/AGENTS.md` is a brief introduction pointing to `~/airlock-machine` and the
full installed guidance. On upgrade, the previous `~/dev/airlock-machine`
checkout moves as a whole, preserving local branches and uncommitted files.
An existing destination or user-written home introduction is preserved.
If you already had a `.venv` in the moved checkout, run
`uv sync --locked --reinstall` once to refresh launchers containing the old path.
