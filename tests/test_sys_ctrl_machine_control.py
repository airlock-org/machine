"""Typed recovery requests must never become arbitrary dom0 commands."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_ctrl_machine_control", ROOT / "lifecycle/runtime/tools/sys-ctrl-machine-control.py"
)
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


@pytest.mark.parametrize("payload", [
    {"action": "shell"},
    {"action": "status", "command": "id"},
    {"action": "release-prune", "release": "/tmp/untrusted", "confirm": "/tmp/untrusted"},
    {"action": "trash-purge", "identity": "../private", "confirm": "../private"},
    {"action": "trash-purge", "identity": "123-usr-home", "confirm": "different"},
    {"action": "status", "before_expiry": True},
])
def test_rejects_untyped_authority(payload):
    with pytest.raises(ValueError):
        CONTROL.arguments({"schema": CONTROL.SCHEMA, **payload})


@pytest.mark.parametrize("action", ["status", "verify", "resume", "rollback", "release-list"])
def test_recovery_is_available_without_a_controller_release(action):
    assert CONTROL.arguments({"schema": CONTROL.SCHEMA, "action": action}) == (action, [action])
