"""Input discovery must keep working devices usable when another device fails."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "lifecycle/runtime/tools/scripts/input-discovery.py"
)
SPEC = importlib.util.spec_from_file_location("input_discovery", SOURCE)
INPUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INPUT)


def device(number, *classes, name="Generic input", path=None):
    return SimpleNamespace(
        device_node=f"/dev/input/event{number}",
        sys_path=path
        or f"/sys/devices/pci0000:00/usb1/1-2/input/input{number}/event{number}",
        properties={f"ID_INPUT_{key}": "1" for key in classes},
        parent=SimpleNamespace(attributes={"name": name.encode()}),
    )


@pytest.mark.parametrize("kind", INPUT.INPUT_CLASSES)
def test_supported_classes_need_no_model_name_or_stable_symlink(kind):
    assert INPUT.describe(device(37, kind))["classes"] == [kind]


def test_composite_device_retains_keyboard_and_absolute_capabilities():
    descriptor = INPUT.describe(device(1, "KEYBOARD", "TOUCHPAD"))
    assert descriptor["classes"] == ["KEYBOARD", "TOUCHPAD"]


def test_non_evdev_sensors_and_synthesized_input_are_ignored():
    mouse = device(1, "MOUSE")
    mouse.device_node = "/dev/input/mouse0"
    assert INPUT.describe(mouse) is None
    assert INPUT.describe(device(2, "ACCELEROMETER")) is None
    assert INPUT.describe(device(3, "SWITCH")) is None
    assert (
        INPUT.describe(
            device(4, "KEYBOARD", path="/sys/devices/virtual/input/input4/event4")
        )
        is None
    )


def test_trusted_inventory_survives_reboot_and_event_renumbering(tmp_path):
    path = tmp_path / "trusted-devices.json"
    first = INPUT.Inventory(path)
    first.remember(INPUT.describe(device(1, "KEYBOARD")))
    second = INPUT.Inventory(path)
    second.remember(INPUT.describe(device(18, "KEYBOARD")))
    second.remember(INPUT.describe(device(19, "MOUSE", name="USB mouse")))
    records = json.loads(path.read_text())["devices"]
    assert len(records) == 2
    assert all(record["trusted"] for record in records.values())
    assert not any("/event" in record["sys_path"] for record in records.values())


class Process:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


def forwarder(monkeypatch, tmp_path, *, broken_node=None):
    launched, stopped = [], []

    def popen(command, **kwargs):
        if broken_node and broken_node in command[4]:
            raise OSError("device disappeared")
        process = Process()
        launched.append((command, process, kwargs))
        return process

    monkeypatch.setattr(INPUT.subprocess, "Popen", popen)
    monkeypatch.setattr(INPUT, "stop_process", stopped.append)
    manager = INPUT.Forwarder(
        "sys-gui", "/qrexec", "/sender", INPUT.Inventory(tmp_path / "inventory.json")
    )
    return manager, launched, stopped


def test_missing_or_failed_pointer_does_not_disable_keyboard(monkeypatch, tmp_path):
    manager, launched, stopped = forwarder(monkeypatch, tmp_path, broken_node="event2")
    manager.add(device(1, "KEYBOARD"))
    manager.add(device(2, "MOUSE"))
    manager.add(device(3, "TOUCHPAD"))
    assert set(manager.processes) == {"/dev/input/event1", "/dev/input/event3"}
    assert len(launched) == 2
    assert stopped == []
    assert all(
        command[-1] == "DEFAULT:QUBESRPC airlock.InputDevice dom0"
        for command, _, _ in launched
    )


def test_removal_replug_and_duplicate_events_are_independent(monkeypatch, tmp_path):
    manager, launched, stopped = forwarder(monkeypatch, tmp_path)
    keyboard, mouse = device(1, "KEYBOARD"), device(2, "MOUSE")
    manager.add(keyboard)
    manager.add(mouse)
    manager.add(mouse)
    assert len(launched) == 2
    manager.remove(mouse.device_node)
    assert stopped == [launched[1][1]]
    assert keyboard.device_node in manager.processes
    manager.add(mouse)
    assert len(launched) == 3
    manager.close()
    assert not manager.processes
    assert len(stopped) == 3


def test_sender_exit_is_logged_without_retrying_or_stopping_others(
    monkeypatch, tmp_path, caplog
):
    manager, launched, stopped = forwarder(monkeypatch, tmp_path)
    manager.add(device(1, "KEYBOARD"))
    mouse = device(2, "MOUSE")
    manager.add(mouse)
    launched[1][1].returncode = 1
    manager.reap()
    manager.add(mouse)
    assert len(launched) == 2
    assert stopped == [launched[1][1]]
    assert list(manager.processes) == ["/dev/input/event1"]
    assert "exited for /dev/input/event2: 1" in caplog.text


def test_unwritable_inventory_does_not_block_input(monkeypatch, tmp_path, caplog):
    manager, launched, _ = forwarder(monkeypatch, tmp_path)
    manager.inventory.path = tmp_path  # A directory cannot be replaced by a file.
    manager.add(device(1, "KEYBOARD"))
    assert len(launched) == 1
    assert "forwarding will continue" in caplog.text


def test_corrupt_inventory_does_not_block_discovery(tmp_path, caplog):
    path = tmp_path / "inventory.json"
    path.write_text("interrupted old record")
    inventory = INPUT.Inventory(path)
    inventory.remember(INPUT.describe(device(1, "KEYBOARD")))
    assert len(json.loads(path.read_text())["devices"]) == 1
    assert "discovery will continue" in caplog.text


def test_monitor_subscribes_before_enumeration_and_handles_late_device(
    monkeypatch, tmp_path
):
    import sys

    manager, launched, stopped = forwarder(monkeypatch, tmp_path)
    order = []
    signals = {}
    keyboard, mouse = device(1, "KEYBOARD"), device(2, "MOUSE")
    mouse.action = "add"

    class Monitor:
        def filter_by(self, **kwargs):
            assert kwargs == {"subsystem": "input"}

        def start(self):
            order.append("subscribe")

        def poll(self, timeout):
            assert timeout == 1
            signals[INPUT.signal.SIGTERM](None, None)
            return mouse

    class Context:
        def list_devices(self, **kwargs):
            order.append("enumerate")
            return [keyboard]

    monkeypatch.setitem(
        sys.modules,
        "pyudev",
        SimpleNamespace(
            Context=Context,
            Monitor=SimpleNamespace(from_netlink=lambda context: Monitor()),
        ),
    )
    monkeypatch.setattr(INPUT, "Forwarder", lambda *args: manager)
    monkeypatch.setattr(
        INPUT.signal, "signal", lambda key, handler: signals.update({key: handler})
    )
    INPUT.run(
        SimpleNamespace(
            target="sys-gui",
            qrexec="/qrexec",
            sender="/sender",
            inventory=tmp_path / "registry",
        )
    )
    assert order == ["subscribe", "enumerate"]
    assert len(launched) == 2
    assert len(stopped) == 2
