# Machine configuration

Edit [src/machine.rs](src/machine.rs) to configure this computer's VMs,
connections and enabled features. [GUI border settings](assets/sys-gui/gnome/gui-style.json)
are the remaining separate machine setting. Shared implementation, validation
machines and operating guidance come from the pinned Airlock application source.

On an installed system this checkout lives at `~/airlock-machine` in
`usr-assistant`. Read [AGENTS.md](AGENTS.md) before changing it. The installed
guide at `/etc/airlock/usr-assistant/AGENTS.md` explains inspection, source
approval, deployment and verification; local commits do not require a GitHub push.

`make prepare` emits an inspectable Nix build tree outside Git. It does not
build or deploy the system. Application dependencies and their offline Cargo
sources are supplied by the installed authoring environment.

Original configuration is [MIT licensed](LICENSE). See [SECURITY.md](SECURITY.md)
and [third-party notices](THIRD_PARTY.md) for security and dependency terms.
