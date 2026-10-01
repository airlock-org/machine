"""Installed interfaces needed before a new VM's host generation boots."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.nix


@pytest.fixture(scope="module")
def interfaces():
    expression = f'''
      let
        f = builtins.getFlake "{ROOT}";
        gui = f.nixosConfigurations.airlock-domain-sys-gui.config;
        host = f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration;
        find = name: packages: builtins.head (builtins.filter
          (p: (p.pname or p.name) == name) packages);
        desktop = find "qubes-desktop-linux-common" gui.environment.systemPackages;
        policy = find "airlock-sys-gui-policy" host.virtualisation.qubes.packages;
        controlPolicy = find "43-airlock-ctrl-guest.policy" host.virtualisation.qubes.packages;
      in {{
        sync = builtins.readFile "${{find "airlock-sync-qube-appmenus" gui.environment.systemPackages}}/bin/airlock-sync-qube-appmenus";
        receiver = builtins.readFile gui.environment.etc."qubes-rpc/qubes.SyncAppMenus".source;
        receiverUser = gui.environment.etc."qubes/rpc-config/qubes.SyncAppMenus".text;
        desktop = builtins.readFile "${{desktop}}/${{gui.nixpkgs.pkgs.python3.sitePackages}}/qubesappmenus/receive.py";
        policy = builtins.readFile "${{policy}}/etc/qubes/policy.d/50-airlock-sys-gui.policy";
        controlPolicy = builtins.readFile "${{controlPolicy}}/etc/qubes/policy.d/43-airlock-ctrl-guest.policy";
        prepare = builtins.readFile host.environment.etc."qubes-rpc/airlock.Ctrl.MachinePrepare".source;
        candidate = builtins.readFile host.environment.etc."qubes-rpc/airlock.Pool.AdmitCandidate".source;
      }}
    '''
    result = subprocess.run([str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
                             "eval", "--impure", "--json", "--expr", expression],
                            cwd=ROOT, check=True, text=True, capture_output=True)
    report = json.loads(result.stdout)
    directory = Path(os.environ["AIRLOCK_LOG_DIR"]) / "live-vm-interfaces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "interfaces.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def test_new_vm_menu_sync_starts_guest_then_uses_upstream_tools(interfaces, tmp_path):
    trace = tmp_path / "trace.jsonl"
    helper = tmp_path / "helper"
    helper.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
with Path(os.environ['TRACE']).open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'remote': os.environ.get('QREXEC_REMOTE_DOMAIN')}) + '\\n')
sys.exit(1 if os.environ.get('FAIL_READY') and 'airlock.AppSessionReady' in sys.argv else 0)
''')
    helper.chmod(0o755)
    source = re.sub(r"/nix/store/[^/]+/bin/(airlock-launch-qube-app|qvm-sync-appmenus)",
                    shlex.quote(str(helper)), interfaces["sync"])
    for failure in (False, True):
        trace.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-c", source, "sync", "usr-new"],
            env={**os.environ, "TRACE": str(trace), "QREXEC_REMOTE_DOMAIN": "dom0",
                 "FAIL_READY": "1" if failure else ""}, capture_output=True, text=True)
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        assert calls[0]["args"] == ["Applications", "usr-new", "airlock.AppSessionReady"]
        if failure:
            assert result.returncode != 0
            assert len(calls) == 1
        else:
            assert result.returncode == 0, result.stderr
            assert calls[1] == {"args": ["--force-rpc", "usr-new"], "remote": None}
    trace.unlink()
    result = subprocess.run(["bash", "-c", source, "sync"],
                            env={**os.environ, "TRACE": str(trace)}, capture_output=True)
    assert result.returncode != 0
    assert not trace.exists()


def test_generated_menu_entries_use_session_barrier_and_fixed_rpc(interfaces):
    assert "Exec=airlock-launch-qube-app Application %VMNAME% " in interfaces["desktop"]
    assert "qubes.StartApp+{}" in interfaces["desktop"]
    assert "airlock.AppSessionReady + sys-gui @tag:guivm-sys-gui allow" in interfaces["policy"]
    assert "airlock.AppSessionReady * @anyvm @anyvm deny" in interfaces["policy"]
    assert "force-user='user'" in interfaces["receiverUser"]
    result = subprocess.run(["bash", "-c", interfaces["receiver"]],
                            env={**os.environ, "QREXEC_SERVICE_ARGUMENT": "unexpected"},
                            capture_output=True)
    assert result.returncode == 2


def test_preparation_and_admission_are_controller_only(interfaces):
    for service in ("airlock.Ctrl.MachinePrepare", "airlock.Pool.AdmitCandidate"):
        assert f"{service} * @anyvm @anyvm deny" in interfaces["controlPolicy"]
    assert "airlock.Ctrl.MachinePrepare + sys-ctrl dom0 allow" in interfaces["controlPolicy"]
    assert "airlock.Pool.AdmitCandidate * sys-ctrl dom0 allow" in interfaces["controlPolicy"]
    for handler in (interfaces["prepare"], interfaces["candidate"]):
        for caller in ("usr-new", "sys-gui", "sys-agent", ""):
            result = subprocess.run(["bash", "-c", handler],
                                    env={**os.environ, "QREXEC_REMOTE_DOMAIN": caller},
                                    capture_output=True)
            assert result.returncode == 1
