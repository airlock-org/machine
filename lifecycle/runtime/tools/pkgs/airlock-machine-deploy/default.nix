{
  lib,
  writeShellApplication,
  python3,
  nix,
  systemd,
  e2fsprogs,
  erofs-utils,
  gnutar,
  lvm2,
  util-linux,
  callPackage,
  ...
}:
let
  storage = python3.pkgs.callPackage ../airlock-qubes-storage { };
  python = python3.withPackages (ps: [ ps.qubes-core-admin-client storage ]);
  source = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../machine-deploy.py
      ../../airlock_state.py
      (lib.fileset.fileFilter (file: file.hasExt "py") ../../airlock_machine_deploy)
    ];
  };
in
writeShellApplication {
  name = "airlock-machine-deploy";
  runtimeInputs = [
    python
    nix
    systemd
    storage
    e2fsprogs
    erofs-utils
    gnutar
    lvm2
    util-linux
    (callPackage ../../../../misc/tools/pkgs/airlock-start-qube-ready { })
    python3.pkgs.qubes-core-admin-client
    python3.pkgs.qubes-core-qrexec.dom0
  ];
  text = ''
    export AIRLOCK_MACHINE_COORDINATOR=${builtins.placeholder "out"}/bin/airlock-machine-deploy
    exec ${python}/bin/python3 ${source}/machine-deploy.py "$@"
  '';
  meta.description = "Repository-owned whole-machine Airlock deployment coordinator";
  meta.license = lib.licenses.mit;
}
