# System workspace in usr-assistant

`usr-assistant` is a separate VM for machine configuration and administration.
Its persistent checkout at `~/airlock-machine` contains the configuration for
the installed machine. Personal applications and files remain in `usr-home`.

Sys-ctrl builds committed configuration; dom0 manages VM lifecycle, policy,
storage, and activation through the existing Qubes interfaces.

## Open the workspace

Open **usr-assistant: Console** from GNOME and enter the checkout:

```sh
cd ~/airlock-machine
```

The checkout and user files are stored on usr-assistant's private volume and
survive system updates. Local configuration changes do not require pushing to
GitHub or checking out the parent `airlock` repository.

## Change the running machine

Inspect the current deployment before preparing a change:

```sh
airlock machine status
airlock machine verify
```

Compare the recorded deployed source with the checkout, including committed and
uncommitted changes. A clean Git tree does not mean its configuration is running.
Resolve older undeployed changes, incomplete deployments, required reboots, or an
unknown source baseline before preparing another update. Recheck before
submission if the checkout or deployment state changes.
Status reports a held machine lock's named owner and reason, with elapsed time
and operation details. Automatic cleanup can temporarily block changes; inspect
that owner rather than treating every busy response as a deployment in progress.
Status and review-status remain available during cleanup. A status response with
`observation_error` is a recorded snapshot with an unsuccessful root-side refresh;
do not interpret it as fresh verification.

Deployment uses a clean, committed checkout. Edit the Nix configuration and
commit the changes, then request a plan:

```sh
airlock machine plan
```

Normal submissions open Airlock Control for
[source approval](controller-review.md) before evaluation and building. Approval
covers the committed source and permitted VM scope. Review the resulting plan,
including affected VMs and any restart or reboot requirement, before applying.
The command returns as soon as the decision is available, allowing the calling
agent to resume reasoning. `approved` authorizes background work; it does not
confirm that the plan or deployment is complete. Use the returned request ID:

```sh
airlock machine review-status --request-id REQUEST_ID
```

Wait for `planned`, then inspect the plan before applying:

```sh
airlock machine apply
# Check the new request ID until deployment_started, then inspect dom0's outcome.
airlock machine review-status --request-id APPLY_REQUEST_ID
airlock machine status
airlock machine verify
```

A successful build or accepted submission does not confirm deployment. Check the
worker outcome and running generations. If dom0 requires a reboot, verify again
after the machine restarts.
On rejection or cancellation, discuss the result with the user instead of
automatically retrying. Resolve failures before submitting further changes.

For changes limited to an existing VM, use `airlock machine guest-plan --target VM`
and `airlock machine guest-apply --target VM`. Use the whole-machine route for
topology, new VMs, routing, preferences, and dom0 policy.

Contributor checks and build instructions are in
[CONTRIBUTING.md](../CONTRIBUTING.md). Agent workflow rules are in
[AGENTS.md](../AGENTS.md).

## Restarts and recovery

Updating usr-assistant closes its running applications. Deployment continues in
a detached dom0 worker. Reopen Console after the VM restarts and inspect the
outcome with `airlock machine status` and `airlock machine verify`.
If using Codex, continue the session with `codex resume`.

Inspect status before using `airlock machine resume` or `airlock machine rollback`.
Status, verification, and recovery remain available when sys-ctrl is stopped.
Rollback restores software and configuration; private data remains at its current
state.

## Stores and source locations

The installed `nix` command uses the pool-backed builder at
`/rw/airlock-builder/merged`. Shared inputs remain pinned independently by dom0.
Workspace state and logs live under `~/.local/state/airlock`.
Do not run external-development GC targets against the usr-assistant builder or
collect the shared pool or dom0 store.

Configuration entry points are `flake.nix`, VM modules in
`lifecycle/misc/modules/`, and VM defaults in `lifecycle/bootstrap/default-domains/`.
See the [builder design](config-system.md) for storage details and the
[external deployment guide](https://github.com/airlock-org/airlock/blob/main/docs/machine-deployment.md)
when working from another computer.

## Optional: using Codex

Start Codex from the configuration checkout:

```sh
cd ~/airlock-machine
codex
```

Codex requires your own login. If your account supports device login, run
`codex login --device-auth` and complete the displayed code in a browser in
usr-home. API-key login is also available. Credentials remain on usr-assistant's
private volume.
See the [authentication guide](https://developers.openai.com/codex/auth/).
