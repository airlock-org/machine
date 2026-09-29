#!/usr/bin/env python3
"""One bounded i8042 recovery for an explicit boot-time keyboard failure."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

CONTROLLER = Path("/sys/bus/platform/drivers/i8042")
FAILURE = re.compile(r"^atkbd serio\d+: Failed to enable keyboard on isa0060/serio\d+$")


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command(argv, timeout=10):
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=True
    )
    return result.stdout


def journal(cursor=None):
    command(["journalctl", "--sync"])
    argv = ["journalctl", "--boot", "--dmesg", "--no-pager", "--output=json"]
    if cursor:
        argv += ["--after-cursor", cursor]
    return [json.loads(line) for line in command(argv).splitlines() if line.strip()]


def failures(records):
    return [
        r
        for r in records
        if isinstance(r.get("MESSAGE"), str) and FAILURE.fullmatch(r["MESSAGE"])
    ]


def keyboard_bound():
    for port in (CONTROLLER / "i8042").glob("serio*"):
        if (port / "driver").resolve().name == "atkbd" and list(
            port.glob("input/input*/name")
        ):
            return True
    return False


def rebind():
    # Keep a potentially blocking sysfs write outside the report-writing process.
    command([sys.executable, str(Path(__file__).resolve()), "--rebind"], timeout=20)


def check(
    report_dir,
    *,
    read_journal=journal,
    reset=rebind,
    bound=keyboard_bound,
    controller_present=None,
    observe_seconds=10,
):
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = report_dir / "input-controller.json"
    with (report_dir / "input-controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(report.read_text()) if report.exists() else {}
        state = {
            **previous,
            "schema": "airlock.input-controller.v1",
            "physical_input_verified": False,
            "attempted": previous.get("attempted", False),
        }

        def save(status, **details):
            state.update(status=status, **details)
            atomic_json(report, state)
            print(json.dumps(state), flush=True)

        try:
            # A killed attempt must never cause another reset on service restart.
            if state["attempted"] and state.get("status") != "reinitialized":
                raise RuntimeError(
                    "previous recovery failed or was interrupted; no second attempt this boot"
                )
            present = (
                (CONTROLLER / "i8042").exists()
                if controller_present is None
                else controller_present
            )
            if not present:
                if state["attempted"]:
                    raise RuntimeError("controller is no longer bound after recovery")
                save("not_applicable")
                return 0
            records = read_journal(state.get("verified_cursor"))
            errors = failures(records)
            if not errors:
                if state["attempted"] and not bound():
                    raise RuntimeError(
                        "reinitialized keyboard driver is no longer bound"
                    )
                save("reinitialized" if state["attempted"] else "no_failure_detected")
                return 0
            atomic_json(report_dir / "input-kernel-before.json", records)
            if state["attempted"]:
                raise RuntimeError(
                    "keyboard initialization failed again; no second attempt this boot"
                )
            cursor = records[-1]["__CURSOR"]
            save("resetting", attempted=True, errors=[r["MESSAGE"] for r in errors])
            reset()
            deadline = time.monotonic() + observe_seconds
            while not bound():
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "keyboard driver did not bind after controller reinitialization"
                    )
                time.sleep(0.1)
            after = read_journal(cursor)
            atomic_json(report_dir / "input-kernel-after.json", after)
            if failures(after):
                raise RuntimeError(
                    "kernel reported keyboard initialization failure after recovery"
                )
            save(
                "reinitialized",
                verified_cursor=after[-1]["__CURSOR"] if after else cursor,
            )
            return 0
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            save("failed", error=str(exc))
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebind", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--finalize", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rebind:
        (CONTROLLER / "unbind").write_text("i8042")
        (CONTROLLER / "bind").write_text("i8042")
        return 0
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    directory = Path("/var/log/airlock/boot-attempts") / boot
    if args.finalize:
        result = os.environ.get("SERVICE_RESULT", "unknown")
        if result != "success":
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / "input-controller.json"
            state = json.loads(path.read_text()) if path.exists() else {}
            state.update(
                status="failed", service_result=result, physical_input_verified=False
            )
            atomic_json(path, state)
        return 0
    return check(directory)


if __name__ == "__main__":
    sys.exit(main())
