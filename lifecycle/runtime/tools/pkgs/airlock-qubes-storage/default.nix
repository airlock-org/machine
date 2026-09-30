{
  buildPythonPackage,
  e2fsprogs,
  erofs-utils,
  gnutar,
  lib,
  lvm2,
  nix,
  makeWrapper,
  setuptools,
  pkgs,
  util-linux,
}:

buildPythonPackage {
  pname = "airlock-qubes-storage";
  version = "0.1.0";
  format = "pyproject";

  src = ./src;

  nativeBuildInputs = [
    makeWrapper
    setuptools
  ];

  postFixup = ''
    wrapProgram "$out/bin/airlock-host-store" \
      --prefix PATH : ${lib.makeBinPath [ nix erofs-utils gnutar lvm2 util-linux pkgs.systemd ]}
    wrapProgram "$out/bin/airlock-closure-store" \
      --prefix PATH : ${lib.makeBinPath [
        e2fsprogs
        erofs-utils
        gnutar
        lvm2
        util-linux
      ]}
  '';

  pythonImportsCheck = [
    "airlock_qubes_storage"
    "airlock_qubes_storage.cli"
    "airlock_qubes_storage._engine.cli"
    "airlock_qubes_storage._engine.views"
    "airlock_qubes_storage._engine.state"
  ];

  meta = {
    description = "Airlock closure-backed Qubes storage adapter and immutable store engine";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
