from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_sys_dev_machine_source_and_build_tools_are_persistent_and_scoped():
    expression = f"""
      let
        flake = builtins.getFlake "{ROOT}";
        guest = flake.nixosConfigurations.airlock-domain-sys-dev.config;
        package = name: builtins.head (builtins.filter (item:
          (item.pname or item.name) == name
        ) guest.environment.systemPackages);
        sourceTool = package "airlock-sys-dev-machine-source";
        buildTool = package "airlock-sys-dev-machine-build";
      in {{
        sourceTool = builtins.readFile "${{sourceTool}}/bin/airlock-sys-dev-machine-source";
        buildTool = builtins.readFile "${{buildTool}}/bin/airlock-sys-dev-machine-build";
        privateStore = guest.airlock.qubeGuest.configNixStoreRoot;
        assertionsPass = builtins.all (entry: entry.assertion) guest.assertions;
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "sys-dev-machine-build"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "report.json").write_text(result.stdout)
    report = json.loads(result.stdout)

    assert report["assertionsPass"] is True
    assert report["privateStore"].startswith("/rw/")
    assert "airlock.sys-dev-machine-source.v1" in report["sourceTool"]
    assert "checkout has local edits; refusing to overwrite it" in report["sourceTool"]
    assert "git bundle verify" in report["sourceTool"]
    assert "private Nix store needs at least 4 GiB free" in report["buildTool"]
    assert "flake metadata --offline" in report["buildTool"]
    assert "airlock-pool-send" in report["buildTool"]
    assert ".activated == false and .retained == true" in report["buildTool"]
