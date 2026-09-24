"""Recoverable startup warnings offer a fixed retry without blocking the desktop."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "lifecycle/runtime/tools/pkgs/airlock-setup/boot-notification.py"
)


@pytest.mark.parametrize("action", ["retry", "dismiss"])
def test_notification_action_and_completion(monkeypatch, action):
    spec = importlib.util.spec_from_file_location("boot_notification", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    states = iter(["working", "degraded", "complete"])

    def rpc(service):
        calls.append(service)
        if service == "airlock.BootStatus":
            return json.dumps({"status": next(states), "desktop_ready": True})
        return ""

    notifications = []

    def notify(args, **kwargs):
        notifications.append(args)
        return SimpleNamespace(stdout=action + "\n")

    monkeypatch.setattr(module, "rpc", rpc)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module.subprocess, "run", notify)
    module.main()
    assert len(notifications) == 1
    assert "--action=retry=Retry" in notifications[0]
    assert ("airlock.BootRetry" in calls) == (action == "retry")
    assert calls.count("airlock.BootStatus") == (3 if action == "retry" else 2)
