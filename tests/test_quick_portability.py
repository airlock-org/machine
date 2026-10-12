"""Quick host observation must also work without optional host tools."""

import json
from pathlib import Path

import machine_pychecks


def test_host_capabilities_without_nix_or_systemd_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    machine_pychecks.check_host_capabilities(Path("."), tmp_path)
    report = json.loads((tmp_path / "host-capabilities.json").read_text())
    assert report["nix"] == {"available": False, "version": ""}
    assert report["host"]["virtualization"] == ""
