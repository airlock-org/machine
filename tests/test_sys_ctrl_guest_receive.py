"""A controller restart cannot cancel an already submitted guest activation."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_ctrl_guest_receive", ROOT / "lifecycle/runtime/tools/sys-ctrl-guest-receive.py"
)
RECEIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIVER)


@pytest.mark.parametrize("action", ["plan", "apply"])
def test_guest_activation_binds_before_detaching(monkeypatch, capsys, action):
    monkeypatch.setattr(RECEIVER.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"generation": 2}\n')))
    candidate = "/var/lib/airlock/machine/candidates/abc"
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"candidate": candidate, "plan": {}}))

    monkeypatch.setattr(RECEIVER.subprocess, "run", run)
    RECEIVER.receive(action, "/coordinator")
    assert calls[0] == ["/coordinator", "guest-plan", "-"]
    result = json.loads(capsys.readouterr().out)
    if action == "apply":
        assert calls[1][0] == "systemd-run"
        assert calls[1][-3:] == ["/coordinator", "apply", candidate]
        assert result["unit"].startswith("airlock-machine-deploy-")
    else:
        assert len(calls) == 1
        assert "unit" not in result


def test_guest_plan_reports_dom0_reason(monkeypatch):
    monkeypatch.setattr(RECEIVER.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"generation": 2}\n')))
    monkeypatch.setattr(
        RECEIVER.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="admitted guest system is already selected\n"
        ),
    )
    with pytest.raises(ValueError, match="admitted guest system is already selected"):
        RECEIVER.receive("plan", "/coordinator")
