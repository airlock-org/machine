{ pkgs }:
let
  python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
in
pkgs.writeShellScriptBin "airlock-setup" ''
  export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" [ pkgs.libadwaita.out pkgs.gtk4.out pkgs.glib.out pkgs.gdk-pixbuf.out pkgs.pango.out pkgs.graphene.out pkgs.harfbuzz.out pkgs.gobject-introspection-unwrapped ]}:''${GI_TYPELIB_PATH:-}"
  export XDG_DATA_DIRS="${pkgs.gsettings-desktop-schemas}/share:${pkgs.adwaita-icon-theme}/share:''${XDG_DATA_DIRS:-/run/current-system/sw/share}"
  export PATH="${pkgs.python3.pkgs.qubes-core-admin-client}/bin:$PATH"
  exec ${python}/bin/python3 ${./gui.py} "$@"
''
