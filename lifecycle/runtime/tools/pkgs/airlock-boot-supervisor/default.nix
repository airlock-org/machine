{
  coreutils,
  lib,
  python3,
  systemd,
  writeShellApplication,
  xen,
  ...
}:
writeShellApplication {
  name = "airlock-boot-supervisor";
  runtimeInputs = [
    coreutils
    systemd
    xen
    python3.pkgs.qubes-core-admin-client
  ];
  text = ''
    exec ${python3}/bin/python3 ${../../boot-supervisor.py} "$@"
  '';
  meta.description = "Persist Airlock boot readiness and recover diagnostics after interrupted boots";
  meta.license = lib.licenses.mit;
}
