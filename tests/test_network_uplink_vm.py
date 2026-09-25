"""Opt-in pytest entry point for the real systemd/udev hotplug regression."""

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.vm
@pytest.mark.slow
@pytest.mark.nix
@pytest.mark.skipif(
    os.environ.get("AIRLOCK_RUN_UPLINK_VM") != "1",
    reason="set AIRLOCK_RUN_UPLINK_VM=1 to run the isolated systemd/udev VM",
)
def test_network_uplink_hotplug():
    log_dir = Path(os.environ.get("AIRLOCK_LOG_DIR", ROOT / ".airlock-logs")) / "uplink-hotplug"
    log_dir.mkdir(parents=True, exist_ok=True)
    expression = f'''let flake = builtins.getFlake "path:{ROOT}";
        in import {ROOT}/tests/fixtures/uplink-hotplug.nix {{ inherit flake; }}'''
    with (log_dir / "build.log").open("w") as log:
        result = subprocess.run(
            ["nix", "build", "--impure", "--no-link", "--print-out-paths", "-L", "--expr", expression],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            timeout=1500,
            check=False,
        )
    (log_dir / "result.txt").write_text(result.stdout)
    assert result.returncode == 0, f"See {log_dir / 'build.log'}"
