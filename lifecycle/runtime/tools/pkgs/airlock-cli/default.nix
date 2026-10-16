{
  coreutils,
  gitMinimal,
  gzip,
  lib,
  python3,
  util-linux,
  writeShellApplication,
  configNixStore ? null,
  commandName ? "airlock",
}:

writeShellApplication {
  name = commandName;

  runtimeInputs = [
    coreutils
    gitMinimal
    gzip
    python3
    util-linux
    python3.pkgs.qubes-core-qrexec.domU
  ] ++ lib.optional (configNixStore != null) configNixStore;

  text = ''
    exec ${python3}/bin/python3 ${../../airlock.py} "$@"
  '';

  meta = {
    description = "Inspect and manage AirlockOS machine configuration and deployment";
    license = lib.licenses.mit;
    mainProgram = commandName;
    platforms = [ "x86_64-linux" ];
  };
}
