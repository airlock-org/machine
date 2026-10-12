{ lib, python3, systemd, coreutils, writeShellApplication }:
writeShellApplication {
  name = "airlock-gpu-recovery";
  runtimeInputs = [ systemd coreutils ];
  text = ''
    exec ${python3}/bin/python3 ${../../gpu-recovery.py} "$@"
  '';
  meta.description = "Manual, bounded recovery diagnostics for the lab sys-gui AMD display";
  meta.license = lib.licenses.mit;
}
