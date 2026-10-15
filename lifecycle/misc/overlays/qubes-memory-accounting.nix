final: prev: {
  qubes-linux-utils = prev.qubes-linux-utils.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ../patches/qubes-linux-utils/0001-account-for-per-cpu-free-pages.patch
    ];
    postPatch = (old.postPatch or "") + ''
      cp ${../patches/qubes-linux-utils/memory-accounting.h} qmemman/memory-accounting.h
    '';
    nativeCheckInputs = (old.nativeCheckInputs or [ ]) ++ [ final.python3Packages.pytest ];
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      export AIRLOCK_MEMINFO_ACCOUNTING_HEADER="$PWD/qmemman/memory-accounting.h"
      python -m pytest -q ${../../../tests/test_memory_accounting.py}
      runHook postCheck
    '';
  });
}
