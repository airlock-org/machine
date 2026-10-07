# Separate services keep the automatic handoff limited to capture delivery and
# opening already received captures, with no arbitrary commands or paths.
builtins.concatMap (service: map (kind: {
  inherit service;
  argument = "+${kind}";
  source = "sys-gui";
  target = "usr-home";
  action = "allow";
}) [ "image" "video" ]) [ "airlock.CaptureImport" "airlock.CaptureOpen" ]
