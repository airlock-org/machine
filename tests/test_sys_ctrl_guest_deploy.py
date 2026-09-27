import importlib.util
import io
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_ctrl_guest_deploy", ROOT / "lifecycle/runtime/tools/sys-ctrl-guest-deploy.py"
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


def test_controller_rejects_unapproved_caller_before_reading_input(monkeypatch):
    monkeypatch.setenv("QREXEC_REMOTE_DOMAIN", "usr-browser")
    with pytest.raises(ValueError, match="only sys-dev"):
        DEPLOY.serve()


@pytest.mark.parametrize("target", ["sys-config-vault", "dom0", "../usr-home"])
def test_submission_rejects_non_guest_targets_before_bundling(target, tmp_path):
    with pytest.raises(ValueError, match="unsupported guest"):
        DEPLOY.request("apply", target, tmp_path)


def test_controller_rejects_unversioned_request_before_import(monkeypatch):
    monkeypatch.setenv("QREXEC_REMOTE_DOMAIN", "sys-dev")
    monkeypatch.setattr(DEPLOY.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{}\n')))
    with pytest.raises(ValueError, match="unsupported request schema"):
        DEPLOY.serve()


@pytest.mark.nix
def test_controller_and_dom0_policy_wiring():
    expression = f'''
      let f = builtins.getFlake "{ROOT}";
          ctrl = f.nixosConfigurations.airlock-domain-sys-ctrl.config;
          host = f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration;
      in {{
        controllerService = toString ctrl.environment.etc."qubes-rpc/airlock.Ctrl.GuestDeploy".source;
        planService = toString host.environment.etc."qubes-rpc/airlock.Ctrl.GuestPlan".source;
        applyService = toString host.environment.etc."qubes-rpc/airlock.Ctrl.GuestApply".source;
        machinePlan = toString host.environment.etc."qubes-rpc/airlock.Ctrl.MachinePlan".source;
        machineApply = toString host.environment.etc."qubes-rpc/airlock.Ctrl.MachineApply".source;
        admissionCaller = host.airlock.poolAdmission.caller;
        builderService = ctrl.systemd.services.airlock-sys-ctrl-build-store.description;
        bootDiagnostics = host.airlock.bootDiagnostics.enable;
      }}
    '''
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert report["admissionCaller"] == "sys-ctrl"
    assert report["bootDiagnostics"] is True
    assert "sys-ctrl" in report["builderService"]
    assert all(report[key].startswith("/nix/store/") for key in
               ("controllerService", "planService", "applyService", "machinePlan", "machineApply"))


@pytest.mark.parametrize("stage", ["inventory", "planning"])
def test_machine_response_preserves_dom0_failure(stage):
    with pytest.raises(ValueError, match="specific admission failure"):
        DEPLOY.machine_response(json.dumps({"schema": DEPLOY.MACHINE_SCHEMA,
                                          "error": "specific admission failure"}), stage)
    with pytest.raises(ValueError, match="closed the machine"):
        DEPLOY.machine_response(b"", stage)
