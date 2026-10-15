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
Source uploads have a two-minute total receive deadline, covering the header and
archive. An incomplete upload releases the controller lock and reports failure;
the later human approval wait has no such deadline. Guest plan/apply failures
return structured errors over qrexec, retaining available lock and exit details.

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

## Decision feedback and background work

Submissions wait for the review decision, then exit so the calling agent can
resume reasoning. A detached user-systemd job continues approved builds and
deployment independently. The qrexec response streams pending JSON states with
a persistent request ID, followed by the decision. Approval exits with code 0:

```json
{"status":"approved","decision":"approved","request_id":"…","job_status":"building","continuing":true,"status_command":"airlock machine review-status --request-id …"}
```

`status` describes the submission's decision; `job_status` is the worker's state
at that moment. `continuing` says whether controller work is still active, not
whether deployment succeeded. The decision is retained even if a fast worker
finishes or fails before the submitter's next poll; check `job_status` and query
the job for its outcome. Approval is never a deployment-success acknowledgement.

Reject, cancellation and failures before approval return their corresponding
status and a nonzero exit code. Resume reasoning from that result; do not
automatically resubmit a rejected candidate.

Disconnecting the submitter does not grant approval or cancel the detached job.
After approval or interruption, query:

```sh
airlock machine review-status --request-id REQUEST_ID
```

Job states include queued, awaiting_approval, building, planned, built,
deployment_started, rejected, cancelled, scope_exceeded, stale_review and failed.
`review-status` reports the current job state and retains `decision: approved`
after approval. Wait for `planned` before inspecting a plan and submitting apply;
do not submit another change while work is pending. Rejection is exit 20,
cancellation 21, scope refusal 22, stale review 23, and failure 1; concurrent
submission returns busy (24). Scope or source failures need a revised proposal.
Use normal status/verify for actual
deployment completion; deployment_started only acknowledges the detached dom0
worker. Status reconciliation detects controller interruption and root-side
scope refusal, including when a VM restart interrupts the original reply.
Queries reconcile the returned snapshot without rewriting the job record; the
controller worker owns its updates. JSON publication uses a unique temporary
file per write so concurrent publications cannot collide on a shared filename.
Guest candidate publication also flushes the containing directory before it is
acknowledged, including when reusing a previously published candidate.
Prepared-plan records use the same writer. Replacements preserve existing file
modes unless the caller explicitly chooses a mode; new private records use
0600, while shared boot/status files explicitly use 0644.

ReviewControl status reads also remain available while deployment or generation
maintenance holds the machine lock. Transport/service failures include the
operation, request ID, exit code and bounded output. Dom0 sends structured errors
on stdout as well as logging them, since qrexec can route service stderr to its
local log instead of back to the caller. If a status observation fails, the
controller returns its recorded job state with `observation_error` and
`control_error`; that warning does not turn an accepted approval into a rejection.
ReviewControl calls time out after ten seconds. Submission feedback returns a
recorded approval before requesting any remote status refresh; use review-status
for a later observation of the background job.
Unreadable or malformed controller job records report their request ID and path
with `recovery_required: true`. Submission stops for explicit investigation and
repair; it never discards or skips damaged history to bypass a pending operation.

## Lock ownership

The controller and dom0 associate exclusive work with a named owner and reason.
For example, `Automatic generation maintenance` holds the machine lock to
`Remove unused generations and collect unused pool artifacts`. Deployment locks
identify their transaction and current phase; controller review locks identify
the submission and whether it is awaiting approval or building.

`airlock machine status` includes the current machine `lock`, or null when free.
Review-status snapshots include `controller_lock` and, when observing an
authorization, the machine `lock`. Busy errors include the same owner details:
reason, operation, operation ID where available, PID, acquisition time, elapsed
seconds, and phase/service unit where available. Inspection does not require
acquiring the exclusive lock. Mutations and approval validation retain their
existing exclusion and authorization checks.

Kernel `flock` remains authoritative. Owner metadata is diagnostic and cannot
grant authority or make a free lock busy. Missing or stale owner details are
reported as unknown while a real lock is held; never delete a lock file or its
metadata to force an operation through. A crashed process releases its kernel
lock, and the next owner replaces any leftover metadata.
Verified ownership also requires the live process's locked descriptor to match
the recorded acquisition marker and lock-file identity. Details that cannot be
verified are labelled unknown, including metadata left from an earlier operation
in the same still-running process. The marker uses a descriptor position, without
writing or extending the lock file.
Metadata publication, process-identity lookup and metadata cleanup failures are
logged as warnings. They cannot abort the protected operation or replace its
original error; kernel exclusion and durable deployment journals remain required.

## Development administration

The development configuration permits dom0 administrative access. This separate
administrative/legacy route remains outside normal submission approval;
this feature does not establish the full production Qubes security boundary.
An explicit development-host `--legacy-dom0` apply uses the coordinator's
`apply --administrative` repair route. It requires dom0 root and the installed
development-mode marker, preserves artifact validation and deployment journaling,
and records `administrative: true` in the transaction. Normal controller requests
cannot select this option; the review marker and Qubes policy remain enabled.
