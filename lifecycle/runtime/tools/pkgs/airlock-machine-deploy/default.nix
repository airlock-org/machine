{
  lib,
  writeShellApplication,
  python3,
  nix,
  systemd,
  ...
}:
let
  storage = python3.pkgs.callPackage ../airlock-qubes-storage { };
  python = python3.withPackages (ps: [ ps.qubes-core-admin-client ]);
in
writeShellApplication {
  name = "airlock-machine-deploy";
  runtimeInputs = [
    python
    nix
    systemd
    storage
    python3.pkgs.qubes-core-admin-client
    python3.pkgs.qubes-core-qrexec.dom0
  ];
  text = ''
    export AIRLOCK_MACHINE_COORDINATOR=${builtins.placeholder "out"}/bin/airlock-machine-deploy
    exec ${python}/bin/python3 ${../../machine-deploy.py} "$@"
  '';
  meta.description = "Repository-owned whole-machine Airlock deployment coordinator";
  meta.license = lib.licenses.mit;
}
