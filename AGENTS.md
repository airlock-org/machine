# Editing this machine

- Edit `src/machine.rs` for VM declarations, connections and enabled features.
  `src/main.rs` is the preparation adapter; shared implementations and validation
  machines belong to the pinned `airlock-config` library.
- Read `/etc/airlock/usr-assistant/AGENTS.md` before inspecting or changing an
  installed system. Follow its baseline, approval, testing and deployment rules;
  a clean checkout or successful build does not establish deployment.
- Preserve Qubes/Xen architecture. Author configuration in Rust/Rusix and keep
  generated Nix outside Git. Commit each coherent configuration change.
- From an Airlock development checkout, follow the parent
  [AGENTS.md](../AGENTS.md), including its Rust standards, and
  [development operations](../docs/development-operations.md). Shared installed
  guidance is authored in `crates/airlock-config/assets/usr-assistant/` there.
- Keep this checkout focused on this computer. Do not add generic application
  code, operating guides or alternative machine examples here.
