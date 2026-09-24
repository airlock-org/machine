{ pkgs }:
let
  python = pkgs.python3;
  script = ./state.py;
in
pkgs.symlinkJoin {
  name = "airlock-setup-state";
  paths = [
    (pkgs.writeShellScriptBin "airlock-setup-status" ''
      exec ${python}/bin/python3 ${script} status
    '')
    (pkgs.writeShellScriptBin "airlock-setup-complete" ''
      exec ${python}/bin/python3 ${script} complete
    '')
  ];
}
