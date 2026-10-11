# Contributing to Airlock Machine

This independently buildable component owns the host/guest Nix configuration,
bootstrap, runtime services and packages, and their tests. Image assembly and
external lab orchestration live in the parent Airlock repository. Read `AGENTS.md`.
Preserve the existing Qubes/Xen architecture and prefer upstreamable changes.

## Local checks

On Linux, prepare Git, Bash, Make, Python >=3.11, uv, and ordinary coreutils and
OpenSSH tools. CI additionally installs jq, util-linux, e2fsprogs, and ripgrep.
Install dependencies explicitly and run the quick suite:

```sh
uv sync --locked
make test-quick
```

The quick suite uses `uv run --no-sync`; it does not provision tools, evaluate
Nix, boot Xen, or require Tailscale/Codex enrollment. Logs fall back to
`.airlock-logs/` without the lab mount; override `AIRLOCK_LOG_DIR` if desired.
New regression tests use pytest and verify behavior and failure boundaries.

For x86_64-linux Nix builds, set `AIRLOCK_DATA_ROOT` to your own storage and use
the repository's Nix wrapper. `make test-nix` is opt-in. The existing machine
uses a ThinkPad hardware example; `lib.mkMachineConfiguration` accepts a
replacement `hardwareModules` list and additional `extraModules`. See
[the parent configuration guide](https://github.com/airlock-org/airlock/blob/main/docs/lab-configuration.md)
or the local parent checkout's `docs/lab-configuration.md`.

## Commits and deployment

Commit each coherent machine change, then update the parent's submodule pointer.
Do not keep editable copies of component helpers or tests in the parent.
Physical deployment from the external computer uses the parent runbook;
installed sys-agent development uses [docs/sys-agent.md](docs/sys-agent.md).
The current request only prepares source locally. A build is not a live deployment.
Record and remove any diagnostic mutation before claiming generation verification.
Use guarded Airlock GC targets only; never collect the host store or guest builder.

Original contributions use the MIT license in `LICENSE`. Preserve third-party
terms and attribution in `THIRD_PARTY.md`; follow `SECURITY.md` for sensitive reports.
