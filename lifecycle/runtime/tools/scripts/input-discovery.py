#!/usr/bin/env python3
"""Discover trusted local evdev devices and forward each through Qubes independently.

Enrollment currently accepts every supported device, including hotplug. The
persistent inventory is a record, not an approval/identity enforcement policy.
"""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time

LOG = logging.getLogger("airlock-input")
INPUT_CLASSES = (
    "KEY",
    "KEYBOARD",
    "MOUSE",
    "POINTINGSTICK",
    "TOUCHPAD",
    "TOUCHSCREEN",
    "TABLET",
    "TABLET_PAD",
    "JOYSTICK",
)


def describe(device):
    """Use udev capabilities, never a model name or an event number, to select input."""
    node = device.device_node or ""
    if not re.fullmatch(r"/dev/input/event[0-9]+", node):
        return None
    if device.sys_path.startswith("/sys/devices/virtual/input/"):
        return None  # Do not recirculate locally synthesized input.
    properties = device.properties
    classes = [key for key in INPUT_CLASSES if properties.get(f"ID_INPUT_{key}") == "1"]
    if not classes:
        return None  # Switches/sensors have no keyboard, relative or absolute input.
    parent = device.parent
    name = parent.attributes.get("name", b"").decode(errors="replace") if parent else ""
    return {
        "name": name,
        "classes": classes,
        "serial": properties.get("ID_SERIAL", ""),
        "path": properties.get("ID_PATH", ""),
        # Fallback also distinguishes interfaces without persistent udev links.
        "sys_path": re.sub(r"/input/input[0-9]+/event[0-9]+$", "", device.sys_path),
        "vendor": properties.get("ID_VENDOR_ID", ""),
        "product": properties.get("ID_MODEL_ID", ""),
    }


class Inventory:
    def __init__(self, path):
        self.path = Path(path)
        self.devices = {}
        try:
            data = json.loads(self.path.read_text())
            if data["version"] != 1 or not isinstance(data["devices"], dict):
                raise ValueError("unsupported inventory")
            self.devices = data["devices"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            LOG.exception("Cannot read input inventory; discovery will continue")

    def remember(self, descriptor):
        identity = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True).encode()
        ).hexdigest()
        if identity in self.devices:
            return
        self.devices[identity] = {
            **descriptor,
            "trusted": True,
            "first_seen": time.time(),
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w") as output:
                json.dump({"version": 1, "devices": self.devices}, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        except OSError:
            # Recording failure must not take the user's keyboard away.
            del self.devices[identity]
            LOG.exception("Cannot persist input inventory; forwarding will continue")


def stop_process(process):
    # qrexec-client spawns the local sender; release both, even if the client exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class Forwarder:
    def __init__(self, target, qrexec, sender, inventory):
        self.target = target
        self.qrexec = qrexec
        self.sender = sender
        self.inventory = inventory
        self.processes = {}
        self.attempted = set()

    def add(self, device):
        node = device.device_node
        if node in self.attempted:
            return
        try:
            descriptor = describe(device)
            if descriptor is None:
                return
            self.inventory.remember(descriptor)
            self.attempted.add(node)
            command = [
                self.qrexec,
                "-d",
                self.target,
                "-l",
                shlex.join([self.sender, node]),
                "DEFAULT:QUBESRPC airlock.InputDevice dom0",
            ]
            self.processes[node] = subprocess.Popen(command, start_new_session=True)
            LOG.info("Forwarding %s (%s)", node, descriptor["name"])
        except (OSError, ValueError):
            LOG.exception("Cannot forward %s; other devices remain connected", node)

    def remove(self, node):
        process = self.processes.pop(node, None)
        if process is not None:
            stop_process(process)
            LOG.info("Released %s", node)
        self.attempted.discard(node)

    def reap(self):
        for node, process in list(self.processes.items()):
            result = process.poll()
            if result is not None:
                LOG.warning("Input forwarding exited for %s: %s", node, result)
                stop_process(process)
                del self.processes[node]
                # No retry storm; unplug/replug or GUI restart permits another attempt.

    def close(self):
        for node in list(self.processes):
            self.remove(node)


def run(args):
    import pyudev

    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="input")
    monitor.start()  # Subscribe before enumeration so late devices cannot be missed.
    forwarder = Forwarder(
        args.target, args.qrexec, args.sender, Inventory(args.inventory)
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for device in context.list_devices(subsystem="input"):
            forwarder.add(device)
        while not stopping:
            device = monitor.poll(timeout=1)
            if device is not None:
                if device.action == "remove":
                    forwarder.remove(device.device_node)
                elif device.action in {"add", "change"}:
                    forwarder.add(device)
            forwarder.reap()
    finally:
        forwarder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--qrexec", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--inventory", required=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
