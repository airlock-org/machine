# usr-assistant workspace

You are in AirlockOS's computer-assistant VM. Help inspect, configure and
maintain the computer through its existing tools and Qubes interfaces.
For machine changes, the editable checkout is `~/airlock-machine`;
start there with `AGENTS.md`, `README.md`,
and `docs/usr-assistant.md` before changing the system.

Full VM, testing, deployment and recovery guidance is in
`/etc/airlock/usr-assistant/AGENTS.md`. Follow the checkout's verification rules:
routine package-list additions/removals skip repository tests; implementation
changes need relevant tests. Commit changes, inspect the deployment plan, and
verify the running generations and requested package state after apply.
