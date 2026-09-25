"""Run inside the disposable NixOS VM: real interface and udev lifecycles."""

import json
import subprocess
import time


def run(*args, check=True, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


def property_value(unit, name):
    return run("systemctl", "show", unit, f"--property={name}", "--value").stdout.strip()


def wait_state(unit, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if property_value(unit, "ActiveState") == expected:
            return
        time.sleep(0.1)
    raise AssertionError(run("systemctl", "status", unit, check=False).stdout)


def settle():
    run("udevadm", "settle", "--timeout=5", timeout=8)


def cleanup(interface, unit):
    # Also release a synchronous udev worker in the negative control by
    # cancelling its pending service start before waiting for event completion.
    run("systemctl", "--no-block", "stop", unit, check=False)
    settle()
    run("ip", "link", "delete", interface, check=False)
    settle()


def test_uplink_configures_again_after_interface_recreation():
    interface = "altest0"
    unit = f"qubes-network-uplink@{interface}.service"
    invocations = set()
    try:
        for _ in range(3):
            run("ip", "link", "add", interface, "type", "dummy")
            settle()
            wait_state(unit, "active")
            invocation = property_value(unit, "InvocationID")
            assert invocation and invocation not in invocations
            invocations.add(invocation)
            address = json.loads(run("ip", "-j", "address", "show", "dev", interface).stdout)[0]
            assert "UP" in address["flags"]
            assert any(entry["local"] == "192.0.2.2" for entry in address["addr_info"])
            run("ip", "link", "delete", interface)
            settle()
            wait_state(unit, "inactive")
    finally:
        cleanup(interface, unit)


def test_device_dependency_control_fails_to_configure_from_udev():
    interface = "alblock0"
    unit = f"airlock-blocking-uplink@{interface}.service"
    try:
        run("ip", "link", "add", interface, "type", "dummy")
        # Depending on systemd's device job handling this can wait for udev
        # or fail the dependency immediately. Neither configures the interface.
        run("udevadm", "settle", "--timeout=3", check=False, timeout=6)
        assert property_value(unit, "ActiveState") != "active"
        address = json.loads(run("ip", "-j", "address", "show", "dev", interface).stdout)[0]
        assert not any(entry["local"] == "192.0.2.2" for entry in address["addr_info"])
        journal = run("journalctl", "-b", "-u", unit, "--no-pager").stdout
        jobs = run("systemctl", "list-jobs", "--no-pager").stdout
        assert "dependency" in journal or (
            unit in jobs and f"sys-subsystem-net-devices-{interface}.device" in jobs
        )
    finally:
        cleanup(interface, unit)
