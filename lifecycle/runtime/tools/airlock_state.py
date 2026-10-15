"""Durable state shared by the controller and machine coordinator."""

import json
import fcntl
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _process_identity(pid):
    return {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "process_start": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
    }


def lock_status(path):
    """Inspect without creating files; stale metadata never makes a lock busy."""
    path = Path(path)
    try:
        stream = path.open("r")
    except FileNotFoundError:
        return None
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(stream, fcntl.LOCK_UN)
            return None
        try:
            owner = json.loads(path.with_name(path.name + ".owner.json").read_text())
            if (not isinstance(owner["pid"], int) or owner["pid"] <= 0
                    or any(not isinstance(owner[key], str) or not owner[key]
                           for key in ("owner", "reason", "operation"))):
                raise ValueError("invalid lock owner")
            identity = _process_identity(owner["pid"])
            if any(owner[key] != value for key, value in identity.items()):
                raise ValueError("stale lock owner")
            elapsed = max(0, time.monotonic() - owner["acquired_monotonic"])
            return {**owner, "held": True, "elapsed_seconds": round(elapsed, 1)}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return {"held": True, "owner": "Unknown operation", "reason": "Owner details unavailable"}


class LockBusy(BlockingIOError):
    def __init__(self, holder):
        self.holder = holder
        holder = holder or {"owner": "Unknown operation", "reason": "Owner details unavailable"}
        detail = f"{holder['owner']}: {holder['reason']}"
        if "pid" in holder:
            detail += f" (PID {holder['pid']}, held {holder['elapsed_seconds']}s)"
        if holder.get("operation_id"):
            detail += "; operation " + str(holder["operation_id"])
        if holder.get("phase"):
            detail += "; phase " + str(holder["phase"])
        super().__init__(detail)


class NamedLock:
    """flock exclusion with descriptive metadata, including across boot exec."""

    def __init__(self, path, *, owner, reason, operation, blocking=False, **context):
        self.path = Path(path)
        self.metadata = self.path.with_name(self.path.name + ".owner.json")
        self.info = {**context, "owner": owner, "reason": reason, "operation": operation}
        self.blocking = blocking

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+")
        try:
            try:
                fcntl.flock(self.stream, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise LockBusy(lock_status(self.path)) from exc
            self.metadata.unlink(missing_ok=True)
            self.info.update(pid=os.getpid(), acquired_at=time.time(),
                             acquired_monotonic=time.monotonic(), **_process_identity(os.getpid()))
            self.update()
            return self
        except BaseException:
            self.stream.close()
            raise

    def update(self, **context):
        self.info.update(context)
        atomic_json(self.metadata, self.info)

    def fileno(self):
        return self.stream.fileno()

    def __exit__(self, *_):
        try:
            self.metadata.unlink(missing_ok=True)
        finally:
            self.stream.close()
