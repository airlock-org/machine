#!/usr/bin/env python3
"""Persistent dom0 first-run setup state."""

import json
import os
from pathlib import Path
import sys
import tempfile

STATE = Path(os.environ.get("AIRLOCK_SETUP_STATE", "/var/lib/airlock/setup/state.json"))


def status():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "incomplete"
    if data != {"schema": 1, "completed": True}:
        raise ValueError("unsupported or invalid setup state")
    return "complete"


def complete():
    if status() == "complete":
        return "complete"
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=STATE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"schema": 1, "completed": True}, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE)
        dir_fd = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return "complete"


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("status", "complete"):
            raise ValueError("expected status or complete")
        print(status() if sys.argv[1] == "status" else complete())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"setup state error: {exc}", file=sys.stderr)
        sys.exit(1)
