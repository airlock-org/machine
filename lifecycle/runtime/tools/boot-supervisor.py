#!/usr/bin/env python3
"""Persistent boot-attempt state and post-freeze diagnostic capture."""

import argparse
import datetime as dt
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess


STATE_ROOT = Path(
    os.environ.get("AIRLOCK_BOOT_STATE_ROOT", "/var/lib/airlock/boot-attempts")
)
LOG_ROOT = Path(
    os.environ.get("AIRLOCK_BOOT_LOG_ROOT", "/var/log/airlock/boot-attempts")
)
RUN_ROOT = Path(os.environ.get("AIRLOCK_BOOT_RUN_ROOT", "/run/airlock/boot-supervisor"))
DIAGNOSTIC_ROOT = Path(os.environ.get("AIRLOCK_BOOT_DIAGNOSTIC_ROOT", "/"))
BOOT_ID_FILE = Path(
    os.environ.get("AIRLOCK_BOOT_ID_FILE", "/proc/sys/kernel/random/boot_id")
)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def boot_id():
    return BOOT_ID_FILE.read_text(encoding="utf-8").strip()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def append_event(identifier, stage, status, detail=""):
    path = LOG_ROOT / identifier / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "boot_id": identifier,
        "time": now(),
        "stage": stage,
        "status": status,
        "detail": detail,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_state(identifier, stage, status, detail="", **extra):
    path = STATE_ROOT / identifier / "state.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    value = {
        **previous,
        "schema": "airlock.boot-attempt.v1",
        "boot_id": identifier,
        "updated_at": now(),
        "stage": stage,
        "status": status,
        "detail": detail,
        **extra,
    }
    value.setdefault("started_at", value["updated_at"])
    atomic_json(path, value)
    append_event(identifier, stage, status, detail)
    return value


def copy_diagnostics(identifier, destination):
    destination.mkdir(parents=True, exist_ok=True)
    patterns = [
        "var/log/xen/console/*.log*",
        "var/log/xen/*.log*",
        "var/log/qubes/*.log*",
        "var/log/airlock/*.json",
        "var/log/airlock/*.log",
    ]
    copied = []
    for pattern in patterns:
        for source_name in sorted(glob.glob(str(DIAGNOSTIC_ROOT / pattern))):
            source = Path(source_name)
            if not source.is_file():
                continue
            target = destination / source.relative_to(DIAGNOSTIC_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, target)
                copied.append(str(source.relative_to(DIAGNOSTIC_ROOT)))
            except OSError as exc:
                with (destination / "copy-errors.log").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(f"{source}: {exc}\n")
    atomic_json(destination / "copied.json", {"boot_id": identifier, "files": copied})


def capture_command(destination, name, command):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
        text = result.stdout + result.stderr
        heading = f"exit={result.returncode} command={json.dumps(command)}\n"
    except (OSError, subprocess.TimeoutExpired) as exc:
        heading = f"command={json.dumps(command)} error={exc}\n"
        text = ""
    (destination / name).write_text(heading + text, encoding="utf-8")


def snapshot(identifier, label, journal_boot=None):
    destination = LOG_ROOT / identifier / "snapshots" / label
    copy_diagnostics(identifier, destination)
    commands = {
        "xl-list.txt": ["xl", "list", "-l"],
        "xl-dmesg.txt": ["xl", "dmesg"],
        "qvm-ls.txt": ["qvm-ls", "--raw-data", "--fields", "NAME,STATE,XID"],
        "failed-units.txt": ["systemctl", "--failed", "--no-pager"],
        "memory.txt": ["free", "-h"],
    }
    if journal_boot:
        commands["journal.txt"] = ["journalctl", "--boot", journal_boot, "--no-pager"]
    else:
        commands["journal.txt"] = ["journalctl", "--boot", "--no-pager"]
    for name, command in commands.items():
        capture_command(destination, name, command)
    return destination


def prepare(_args):
    identifier = boot_id()
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    marker = STATE_ROOT / "in-progress.json"
    previous = json.loads(marker.read_text()) if marker.exists() else None
    recovered = None
    if previous and previous.get("boot_id") != identifier:
        recovered = previous["boot_id"]
        snapshot(recovered, "recovered-on-" + identifier, journal_boot=recovered)
        write_state(
            recovered,
            previous.get("stage", "unknown"),
            "interrupted",
            "boot ended before readiness was confirmed",
            recovered_by=identifier,
        )
        atomic_json(
            RUN_ROOT / "recovery-required.json",
            {
                "schema": "airlock.boot-recovery.v1",
                "failed_boot_id": recovered,
                "boot_id": identifier,
            },
        )
        marker.unlink(missing_ok=True)
        write_state(
            identifier,
            "recovery",
            "recovery-required",
            f"previous boot {recovered} did not complete",
            previous_boot_id=recovered,
        )
    else:
        state = write_state(
            identifier, "prepare", "working", "boot supervision started"
        )
        atomic_json(marker, state)
    atomic_json(
        RUN_ROOT / "current.json", {"boot_id": identifier, "recovered": recovered}
    )
    return 0


def update_marker(value):
    marker = STATE_ROOT / "in-progress.json"
    if (
        marker.exists()
        and json.loads(marker.read_text()).get("boot_id") == value["boot_id"]
    ):
        atomic_json(marker, value)


def stage(args):
    identifier = boot_id()
    value = write_state(identifier, args.name, "working", args.detail)
    update_marker(value)
    return 0


def fail(args):
    identifier = boot_id()
    value = write_state(identifier, args.name, "failed", args.detail)
    update_marker(value)
    snapshot(identifier, "failure-" + args.name.replace("/", "_"))
    return 0


def complete(args):
    identifier = boot_id()
    write_state(identifier, "ready", "complete", args.detail)
    marker = STATE_ROOT / "in-progress.json"
    if marker.exists() and json.loads(marker.read_text()).get("boot_id") == identifier:
        marker.unlink()
    snapshot(identifier, "ready")
    return 0


def recovery_ack(_args):
    identifier = boot_id()
    write_state(identifier, "recovery", "complete", "safe recovery console reached")
    # Keep the runtime gate for this boot, but allow the next boot to try normally.
    marker = STATE_ROOT / "in-progress.json"
    marker.unlink(missing_ok=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare")
    snapshot_parser = sub.add_parser("snapshot")
    snapshot_parser.add_argument("label")
    for action in ("stage", "fail"):
        item = sub.add_parser(action)
        item.add_argument("name")
        item.add_argument("detail", nargs="?", default="")
    done = sub.add_parser("complete")
    done.add_argument("detail", nargs="?", default="all required guests are ready")
    sub.add_parser("recovery-ack")
    args = parser.parse_args(argv)
    if args.action == "prepare":
        return prepare(args)
    if args.action == "snapshot":
        snapshot(boot_id(), args.label)
        return 0
    if args.action == "stage":
        return stage(args)
    if args.action == "fail":
        return fail(args)
    if args.action == "complete":
        return complete(args)
    return recovery_ack(args)


if __name__ == "__main__":
    raise SystemExit(main())
