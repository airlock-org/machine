"""Opt-in integration checks for the reporter package and guest wiring."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.nix


def test_memory_accounting_package_builds_and_runs_c_regressions():
    subprocess.run([
        "nix", "build", "--no-link", ".#checks.x86_64-linux.qubes-memory-accounting",
    ], cwd=ROOT, check=True, capture_output=True, text=True)


def test_prepared_guests_use_the_patched_reporter():
    expression = f'''
      let f = builtins.getFlake "{ROOT}";
          patches = name: map builtins.baseNameOf
            f.nixosConfigurations.${{name}}.config.nixpkgs.pkgs.qubes-linux-utils.patches;
      in {{
        assistant = patches "airlock-domain-usr-assistant";
        home = patches "airlock-domain-usr-home";
        filteredTestIncluded = builtins.pathExists
          (f.airlock.configInputs.airlockSupport + "/tests/test_memory_accounting.py");
      }}
    '''
    result = subprocess.run([
        "nix", "eval", "--impure", "--json", "--expr", expression,
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert report.pop("filteredTestIncluded") is True
    for patches in report.values():
        assert "0001-account-for-per-cpu-free-pages.patch" in patches
