# Development workspace in usr-assistant

`usr-assistant` is a separate development domain with a persistent machine
checkout and command-line tools, including Codex in Console. Everyday
applications and personal files remain in `usr-home`.

System changes use the existing Qubes mechanisms. Sys-ctrl validates and builds
committed source; dom0 retains policy, lifecycle, storage, and activation
authority. Development-specific dom0 access is explicitly configured.

Search GNOME for **usr-assistant: Console**. Console is the ordinary GNOME Console
application running inside usr-assistant, with its windows forwarded through Qubes
to sys-gui. It is discovered from the VM's own desktop entries, like apps in
usr-home. Open it and start Codex in the persistent machine checkout:

```sh
cd ~/airlock-machine
codex
```

Codex, Console, Git, ripgrep, Make, Python, uv, and Zellij are installed through
Nix. The checkout, login credentials, session history, and Python dependency
cache live on usr-assistant's private volume and survive root-generation updates.
The existing upstream remote is not needed to commit and deploy locally.

On x86_64 Linux, Codex is pinned to the complete upstream `0.162.1` release
package by `lifecycle/misc/overlays/codex-release.nix`. Its CLI, code-mode host,
sandbox tools and other package-relative runtimes are installed together;
glibc-linked runtime helpers are adapted to Nix during the build. Update the
version and archive hash in `lifecycle/runtime/tools/pkgs/codex-release/default.nix`,
then commit, deploy and verify `codex --version` in the running usr-assistant.
An npm or home-directory replacement would not update this managed package.

Usr-assistant uses PVH with no assigned PCI devices. Sys-net owns the physical
network hardware; usr-assistant reaches the internet through sys-firewall.

## First use

Codex needs your own login. For a ChatGPT account with device login enabled,
run `codex login --device-auth` in usr-assistant, then complete the displayed code
using GNOME Web in usr-home. API-key login is also available. Credentials remain
inside usr-assistant; the system does not import another VM's browser profile or
embed authentication in the machine configuration.

`~/AGENTS.md` is a brief introduction pointing to `~/airlock-machine` and the
full installed guidance. On upgrade, the previous `~/dev/airlock-machine`
checkout moves as a whole, preserving local branches and uncommitted files.
An existing destination or user-written home introduction is preserved.
If you already had a `.venv` in the moved checkout, run
`uv sync --locked --reinstall` once to refresh launchers containing the old path.

`CODEX_HOME` points at `~/.codex`. On first boot, a symlink there named
`AGENTS.md` points at `/etc/airlock/usr-assistant/AGENTS.md`, which contains the VM
map, source locations, tests, deployment, observation, and recovery commands.
It follows the selected system generation. An existing user-written file or
override is preserved. The checkout's own `AGENTS.md` supplies project rules.
Official setup references: [authentication](https://developers.openai.com/codex/auth/)
and [instruction discovery](https://developers.openai.com/codex/guides/agents-md/).

Setup fills missing Git name and email values with `Airlock User` and
`user@airlock.invalid` in `~/airlock-machine` only. It runs after source seeding
and on subsequent boots, preserving existing local or global identity settings.
To use a personal identity instead:

```sh
git -C ~/airlock-machine config --local user.name 'Your Name'
git -C ~/airlock-machine config --local user.email 'you@example.com'
```

Create a branch when the seeded checkout is detached. Prepare pinned
Python dependencies with `uv sync --locked`, then run `make test-quick`.
Tests use the installed Python, with automatic Python downloads disabled.
Reports go to `$AIRLOCK_LOG_DIR` (`~/.local/state/airlock/logs`).

## Change the running machine

Edit and test this checkout, commit the change, then inspect the normal plan:

```sh
airlock-usr-assistant-machine-deploy plan
airlock-usr-assistant-machine-deploy apply
airlock-usr-assistant-machine-deploy status
airlock-usr-assistant-machine-deploy verify
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

Updating usr-assistant closes its Console and Codex processes. Deployment continues
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
