"""Recovery must be bounded, evidence-driven, and never imply physical input works."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "lifecycle/runtime/tools/scripts/input-boot-guard.py"
)
SPEC = importlib.util.spec_from_file_location("input_boot_guard", SOURCE)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
ERROR = {
    "__CURSOR": "old",
    "MESSAGE": "atkbd serio0: Failed to enable keyboard on isa0060/serio0",
}
GOOD = {"__CURSOR": "new", "MESSAGE": "input: AT Translated Set 2 keyboard"}


def report(path):
    return json.loads((path / "input-controller.json").read_text())


@pytest.mark.parametrize(
    "records",
    [
        [],
        [GOOD],
        [{"MESSAGE": "atkbd serio0: Failed to deactivate keyboard on isa0060/serio0"}],
    ],
)
def test_no_reset_without_explicit_enable_failure(tmp_path, records):
    def unexpected():
        pytest.fail("unnecessary controller reset")

    assert (
        GUARD.check(
            tmp_path,
            read_journal=lambda _: records,
            reset=unexpected,
            controller_present=True,
        )
        == 0
    )
    assert report(tmp_path)["status"] == "no_failure_detected"
    assert report(tmp_path)["physical_input_verified"] is False


def test_machine_without_controller_does_not_need_keyboard(tmp_path):
    assert GUARD.check(tmp_path, controller_present=False) == 0
    assert report(tmp_path)["status"] == "not_applicable"


def test_rebind_preserves_evidence_before_mutation_and_never_repeats(tmp_path):
    resets = []
    cursors = []

    def journal(cursor):
        cursors.append(cursor)
        return [ERROR] if cursor is None else [GOOD]

    def reset():
        assert report(tmp_path)["status"] == "resetting"
        assert report(tmp_path)["attempted"] is True
        assert json.loads((tmp_path / "input-kernel-before.json").read_text()) == [
            ERROR
        ]
        resets.append(1)

    kwargs = dict(
        read_journal=journal, reset=reset, bound=lambda: True, controller_present=True
    )
    assert GUARD.check(tmp_path, **kwargs) == 0
    assert report(tmp_path)["status"] == "reinitialized"
    assert report(tmp_path)["physical_input_verified"] is False
    assert GUARD.check(tmp_path, **kwargs) == 0
    assert cursors == [None, "old", "new"]
    assert resets == [1]


@pytest.mark.parametrize("failure", ["timeout", "write", "unbound", "kernel"])
def test_failure_is_durable_and_restart_cannot_reset_again(tmp_path, failure):
    resets = []

    def reset():
        resets.append(1)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("rebind", 20)
        if failure == "write":
            raise OSError("controller unavailable")

    kwargs = dict(
        read_journal=lambda cursor: (
            [ERROR] if cursor is None or failure == "kernel" else [GOOD]
        ),
        reset=reset,
        bound=lambda: failure != "unbound",
        controller_present=True,
        observe_seconds=0,
    )
    assert GUARD.check(tmp_path, **kwargs) == 1
    assert report(tmp_path)["status"] == "failed"
    assert GUARD.check(tmp_path, **kwargs) == 1
    assert resets == [1]


def test_interrupted_attempt_is_not_repeated(tmp_path):
    (tmp_path / "input-controller.json").write_text(
        json.dumps({"status": "resetting", "attempted": True})
    )
    assert GUARD.check(tmp_path, controller_present=True) == 1
    assert "no second attempt" in report(tmp_path)["error"]


def test_journal_failure_cannot_report_healthy(tmp_path):
    def unavailable(_):
        raise subprocess.TimeoutExpired("journalctl", 10)

    assert GUARD.check(tmp_path, read_journal=unavailable, controller_present=True) == 1
    assert report(tmp_path)["status"] == "failed"
    assert report(tmp_path)["attempted"] is False


def test_new_failure_after_recovery_does_not_reset_again(tmp_path):
    (tmp_path / "input-controller.json").write_text(
        json.dumps(
            {"status": "reinitialized", "attempted": True, "verified_cursor": "old"}
        )
    )
    assert (
        GUARD.check(tmp_path, read_journal=lambda _: [ERROR], controller_present=True)
        == 1
    )
    assert "no second attempt" in report(tmp_path)["error"]
