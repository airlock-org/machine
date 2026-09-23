{
  cfg,
  lib,
  pkgs,
  airlockPrebuiltTemplateSources,
}:
rec {
  initialRuntimeConfigFlake = builtins.readFile ../../../bootstrap/initial-runtime-config/flake.nix;
  initialRuntimeConfigVms = builtins.readFile ../../../bootstrap/initial-runtime-config/airlock/config-vms.nix;
  initialRuntimeDom0Host = builtins.readFile ../../../bootstrap/initial-runtime-config/airlock/dom0-host.nix;
  initialRuntimeDom0ProfileActivate = builtins.readFile ../../../bootstrap/initial-runtime-config/dom0-profile/bin/airlock-dom0-profile-activate;
  initialRuntimeDom0ProfileJson = builtins.readFile ../../../bootstrap/initial-runtime-config/dom0-profile/share/airlock/profile.json;
  initialRuntimeLocal = builtins.readFile ../../../bootstrap/initial-runtime-config/airlock/local.nix;
  initialRuntimeConfig = builtins.readFile ../../../bootstrap/initial-runtime-config/airlock/runtime.nix;
  initialRuntimeSystem = builtins.readFile ../../../bootstrap/initial-runtime-config/airlock/system.nix;
  initialRuntimeSystemSchema = builtins.readFile ../../lib/system.nix;
  initialRuntimeTopologySchema = builtins.readFile ../../lib/topology.nix;
  initialRuntimePrebuiltTemplateSources = lib.generators.toPretty { } airlockPrebuiltTemplateSources;
  initialRuntimeConfigRepoPaths = [
    "README.md"
    "dom0-profile/bin/airlock-dom0-profile-activate"
    "dom0-profile/share/airlock/profile.json"
    "flake.nix"
    "airlock/config-vms.nix"
    "airlock/dom0-host.nix"
    "airlock/local.nix"
    "airlock/runtime.nix"
    "airlock/prebuilt-template-sources.nix"
    "airlock/system.nix"
    "airlock/system-schema.nix"
    "airlock/topology.nix"
  ];
  initialRuntimeConfigRepoGitAddArgs = lib.escapeShellArgs initialRuntimeConfigRepoPaths;
  initialRuntimeConfigRepoFilesScript = ''
    write_initial_runtime_config_repo_files() {
      mkdir -p dom0-profile/bin dom0-profile/share/airlock airlock
      cat > README.md <<'EOF'
    # Airlock Runtime Configuration

    This is the editable Airlock configuration candidate. dom0 archives a
    validated commit into sys-config-vault before attempting deployment.
    EOF
      cat > flake.nix <<'EOF'
    ${initialRuntimeConfigFlake}
    EOF
      cat > airlock/topology.nix <<'EOF'
    ${initialRuntimeTopologySchema}
    EOF
      cat > airlock/system-schema.nix <<'EOF'
    ${initialRuntimeSystemSchema}
    EOF
      cat > airlock/config-vms.nix <<'EOF'
    ${initialRuntimeConfigVms}
    EOF
      cat > airlock/dom0-host.nix <<'EOF'
    ${initialRuntimeDom0Host}
    EOF
      cat > airlock/local.nix <<'EOF'
    ${initialRuntimeLocal}
    EOF
      cat > airlock/runtime.nix <<'EOF'
    ${initialRuntimeConfig}
    EOF
      cat > airlock/system.nix <<'EOF'
    ${initialRuntimeSystem}
    EOF
      cat > airlock/prebuilt-template-sources.nix <<'EOF'
    ${initialRuntimePrebuiltTemplateSources}
    EOF
      cat > dom0-profile/bin/airlock-dom0-profile-activate <<'EOF'
    ${initialRuntimeDom0ProfileActivate}
    EOF
      cat > dom0-profile/share/airlock/profile.json <<'EOF'
    ${initialRuntimeDom0ProfileJson}
    EOF
      chmod 0755 dom0-profile/bin/airlock-dom0-profile-activate
    }
  '';
  configInputPathSubstitutionScript = ''
    substitute_config_inputs() {
      config_inputs_root="$1"
      ${pkgs.gnused}/bin/sed -i \
        -e "s|__AIRLOCK_NIXPKGS_URL__|path:$config_inputs_root/nixpkgs|g" \
        -e "s|__NIXPKGS_LIBVIRT_10_5_PATH__|$config_inputs_root/nixpkgs-libvirt_10_5|g" \
        -e "s|__AIRLOCK_NIXOS_QUBES_PATH__|$config_inputs_root/nixos-qubes|g" \
        -e "s|__AIRLOCK_SUPPORT_PATH__|$config_inputs_root/airlock-support|g" \
        flake.nix
      for old_config_inputs_root in \
        /rw/airlock/config-inputs \
        /rw/home/${lib.escapeShellArg cfg.user}/.local/share/airlock/config-inputs \
        ${lib.escapeShellArg cfg.configInputsPath}; do
        ${pkgs.gnused}/bin/sed -i \
          -e "s|path:$old_config_inputs_root/nixpkgs|path:$config_inputs_root/nixpkgs|g" \
          -e "s|$old_config_inputs_root/nixpkgs-libvirt_10_5|$config_inputs_root/nixpkgs-libvirt_10_5|g" \
          -e "s|$old_config_inputs_root/nixos-qubes|$config_inputs_root/nixos-qubes|g" \
          -e "s|$old_config_inputs_root/airlock-support|$config_inputs_root/airlock-support|g" \
          flake.nix
      done
    }
  '';
}
