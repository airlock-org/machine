{
  lib,
  stdenv,
  stdenvNoCC,
  fetchurl,
  autoPatchelfHook,
  alsa-lib,
  ncurses,
}:
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "codex";
  version = "0.162.1";
  src = fetchurl {
    url = "https://github.com/openai/codex/releases/download/rust-v${finalAttrs.version}/codex-package-x86_64-unknown-linux-musl.tar.gz";
    hash = "sha256-pnb1ciquDYbL43ZDMvCOqx/didwKMAs61P+/SGEfo+M=";
  };
  sourceRoot = ".";
  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ stdenv.cc.cc.lib alsa-lib ncurses ];
  dontBuild = true;
  dontStrip = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    # Preserve upstream's complete package layout and unwrapped static CLI:
    # daemon validation checks entrypoint identity and package-relative helpers.
    cp -a bin codex-package.json codex-path codex-resources "$out/"
    runHook postInstall
  '';
  # The bundled voice runtime includes glibc-linked binaries/libraries. Strict
  # autoPatchelf adapts their loader and dependencies to Nix; static musl CLI,
  # code-mode host and sandbox binaries retain their upstream bytes.
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    test "$($out/bin/codex --version)" = "codex-cli ${finalAttrs.version}"
    "$out/bin/codex" --help > /dev/null
    test -x "$out/bin/codex-code-mode-host"
    test -x "$out/codex-path/rg"
    test -x "$out/codex-resources/bwrap"
    "$out/codex-resources/zsh/bin/zsh" --version
    "$out/codex-path/rg" --version > /dev/null
    "$out/codex-resources/bwrap" --version
    runHook postInstallCheck
  '';
  meta = {
    description = "OpenAI Codex CLI with its complete upstream Linux runtime package";
    homepage = "https://developers.openai.com/codex/cli/";
    license = lib.licenses.asl20;
    mainProgram = "codex";
    platforms = [ "x86_64-linux" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
