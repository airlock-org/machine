# Controller source approval

Normal submissions from usr-assistant require one human approval before
sys-ctrl evaluates or builds the committed source. The installed Airlock Control
application runs in sys-ctrl and forwards its window through the existing Qubes
GUI agent to sys-gui. App source is a pinned monorepo input, not an adjacent
checkout or a copy edited inside the machine repository.

In GNOME, sys-gui switches to a newly opened review's workspace and focuses the
Airlock Control window once, after checking its Qubes VM origin and application
class. Reviews that arrive while the desktop is locked wait until unlock.
Routine refreshes and extension reloads do not reactivate an existing window.

Images embed the machine Git revision and lock hash in the host
generation's `etc/airlock/machine-source.json`. Dom0 uses that identity for
the initial review baseline before any release has been selected. The boot
menu's source label can identify the parent image repository and is separate.
Missing or invalid machine provenance blocks review; a mutable guest checkout
cannot supply the deployed baseline. Existing images without this metadata
need a rebuilt image or a separately verified administrative upgrade.

Source review compares the submitted source with that deployed baseline. Earlier
committed but undeployed system changes can therefore appear alongside a new
package request. Before editing and again before submission, follow the
[pending-change check](usr-assistant.md#change-the-running-machine); stop for
older deployable work instead of combining it with the new request.

## Approval and scope

The controller validates/imports the Git bundle without evaluating Nix, obtains
the selected source baseline from dom0 and opens the source review. The permitted
impact names VMs that may change/start/stop/restart. Reboot is informational, with
no separate permission control: Approve covers changes that require a reboot.
The exact deployment plan is unknown until building completes, so the review
warns that a reboot may be required. The final plan confirms the requirement;
activation leaves a required reboot pending until the user restarts the machine.
Approve authorizes evaluation, building and deployment of that revision and lock
hash within the displayed VM scope; Reject or closing the window returns feedback
without building. No second routine approval is required when a plan is followed by apply for the same source and baseline.

## Enforcement

Dom0 retains the authorization in protected machine state. Submissions cannot
provide an approval, and ordinary local GUI receipts grant no authority. The
controller rechecks source identity after approval, preserves the lock file and
uses Nix sandboxing. Dom0 binds independently validated artifacts to the grant,
checks the final plan against the scope, then checks again under the activation
lock. Changed source, lock hash, selected baseline or boot invalidates the grant.
Unpermitted VM impacts stop deployment. Existing approvals with an explicit
reboot restriction retain that restriction; they are not silently broadened.
The normal review route uses the installed coordinator, not a candidate-provided executable.
Existing typed resume/rollback can recover already selected generations when
sys-ctrl is stopped.

## Interrupted submissions

Submissions wait while a detached user-systemd job carries out review and build.
The qrexec response streams JSON state changes with a persistent request ID.
Disconnecting the submitter does not grant approval or cancel the detached job.
After interruption, query:

```sh
airlock machine review-status --request-id REQUEST_ID
```

States include queued, awaiting_approval, building, planned, built,
deployment_started, rejected, cancelled, scope_exceeded, stale_review and failed.
Negative decisions have distinct nonzero exit codes (20–23); concurrent submission
returns busy (24). Do not automatically resubmit a rejected candidate. Scope or
source failures need a revised proposal. Use normal status/verify for actual
deployment completion; deployment_started only acknowledges the detached dom0
worker. Status reconciliation detects controller interruption and root-side
scope refusal, including when a VM restart interrupts the original reply.

## Development administration

The development configuration permits dom0 administrative access. This separate
administrative/legacy route remains outside normal submission approval;
this feature does not establish the full production Qubes security boundary.
An explicit development-host `--legacy-dom0` apply uses the coordinator's
`apply --administrative` repair route. It requires dom0 root and the installed
development-mode marker, preserves artifact validation and deployment journaling,
and records `administrative: true` in the transaction. Normal controller requests
cannot select this option; the review marker and Qubes policy remain enabled.
