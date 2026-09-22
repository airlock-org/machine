{
  coreutils,
  jq,
  lib,
  python3,
  writeShellApplication,
}:
writeShellApplication {
  name = "airlock-start-qube-ready";
  runtimeInputs = [
    coreutils
    jq
    python3.pkgs.qubes-core-admin-client
    python3.pkgs.qubes-core-qrexec.dom0
  ];
  text = builtins.readFile ../../scripts/start-qube-ready.sh;
  meta.description = "Start a Qube and require exact-generation qrexec readiness";
  meta.license = lib.licenses.mit;
  meta.platforms = [ "x86_64-linux" ];
}
