"""Dom0 setup state must survive restarts and reject damaged records."""

import importlib.util
import json
from pathlib import Path

import pytest


SOURCE = (
    Path(__file__).parents[1]
    / "lifecycle/runtime/tools/pkgs/airlock-setup-state/state.py"
)


@pytest.fixture
def state(tmp_path):
    spec = importlib.util.spec_from_file_location("airlock_setup_state", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = tmp_path / "setup" / "state.json"
    return module


def test_fresh_completion_is_durable_and_idempotent(state):
    assert state.status() == "incomplete"
    assert not state.STATE.exists()
    assert state.complete() == "complete"
    assert state.status() == "complete"
    assert json.loads(state.STATE.read_text()) == {"schema": 1, "completed": True}
    assert state.STATE.stat().st_mode & 0o777 == 0o600
    assert state.complete() == "complete"


def test_invalid_record_is_not_overwritten(state):
    state.STATE.parent.mkdir()
    state.STATE.write_text("corrupt")
    with pytest.raises(json.JSONDecodeError):
        state.complete()
    assert state.STATE.read_text() == "corrupt"


def test_failed_replace_does_not_report_completion(state, monkeypatch):
    def fail_replace(_source, _target):
        raise OSError("disk failure")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        state.complete()
    assert state.status() == "incomplete"
    assert list(state.STATE.parent.glob(".state-*")) == []
