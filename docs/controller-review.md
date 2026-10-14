# Controller source approval

Normal submissions from usr-assistant now require one human approval before
sys-ctrl evaluates or builds the committed source. The installed Airlock Control
application runs in sys-ctrl and forwards its window through the existing Qubes
GUI agent to sys-gui. App source is a pinned monorepo input, not an adjacent
checkout or a copy edited inside the machine repository.

The controller validates/imports the Git bundle without evaluating Nix, obtains
the selected source baseline from dom0 and opens the source review. The permitted
impact names VMs that may change/start/stop/restart and whether a system reboot
is permitted. Reboot permission defaults to false. The exact deployment plan is
unknown until building completes. Approve authorizes evaluation, building and
deployment of that revision and lock hash within the displayed scope; Reject or
closing the window returns feedback without building. No second routine approval
is required when a plan is followed by apply for the same source and baseline.

Dom0 retains the authorization in protected machine state. Submissions cannot
provide an approval, and ordinary local GUI receipts grant no authority. The
controller rechecks source identity after approval, preserves the lock file and
uses Nix sandboxing. Dom0 binds independently validated artifacts to the grant,
checks the final plan against the scope, then checks again under the activation
lock. Changed source, lock hash, selected baseline or boot invalidates the grant.
Unpermitted VM impacts or host reboot requirements stop deployment. The normal
review route uses the installed coordinator, not a candidate-provided executable.
Existing typed resume/rollback can recover already selected generations when
sys-ctrl is stopped.

Submissions wait while a detached user-systemd job carries out review and build.
The qrexec response streams JSON state changes with a persistent request ID.
Disconnecting the submitter does not grant approval or cancel the detached job.
After interruption, query:

```sh
airlock-usr-assistant-machine-deploy review-status --request-id REQUEST_ID
```

States include queued, awaiting_approval, building, planned, built,
deployment_started, rejected, cancelled, scope_exceeded, stale_review and failed.
Negative decisions have distinct nonzero exit codes (20–23); concurrent submission
returns busy (24). Do not automatically resubmit a rejected candidate. Scope or
source failures need a revised proposal. Use normal status/verify for actual
deployment completion; deployment_started only acknowledges the detached dom0
worker. Status reconciliation detects controller interruption and root-side
scope refusal, including when a VM restart interrupts the original reply.

The physical lab still exposes development dom0 administrative access. That
separate administrative/legacy route remains outside normal submission approval;
this feature does not establish the full production Qubes security boundary.
