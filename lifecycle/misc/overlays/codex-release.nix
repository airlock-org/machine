final: prev:
{
  codex =
    if prev.stdenv.hostPlatform.system == "x86_64-linux" then
      final.callPackage ../../runtime/tools/pkgs/codex-release { }
    else
      prev.codex;
}
