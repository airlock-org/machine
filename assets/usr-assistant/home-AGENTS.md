# usr-assistant workspace

You are in AirlockOS's system workspace VM. Help inspect, configure and
maintain the computer through its existing tools and Qubes interfaces.
For machine changes, the editable checkout is `~/airlock-machine`;
start there with `AGENTS.md`, `README.md`,
and `scripts/runtime/README.md` before changing the system.

Before a new system edit, compare the checkout with the recorded deployed source
and check deployment status. Stop and report older undeployed system changes,
unfinished/failed deployments or an unknown baseline; let the user choose how to
resolve them. Recheck before submission. Documentation/instruction-only changes
do not require deployment. See the checkout's `AGENTS.md` for the full rule.

Full VM, testing, deployment and recovery guidance is in
`/etc/airlock/usr-assistant/AGENTS.md`. Follow the checkout's verification rules:
routine package-list additions/removals skip repository tests; implementation
changes need relevant tests. Commit changes, inspect the deployment plan, and
verify the running generations and requested package state after apply.
Routine installs/removals do not need documentation edits unless they change
documented behavior, instructions or supported capabilities. Keep package selections
in Nix.

Deployment submissions return after the human review decision. An `approved`
reply lets you resume reasoning while sys-ctrl continues work; it does not prove
deployment success. Follow the returned request ID with
`airlock machine review-status --request-id REQUEST_ID`, wait for a plan before
apply, and verify the deployment outcome before making further system changes.
Discuss rejection or cancellation instead of automatically resubmitting.
