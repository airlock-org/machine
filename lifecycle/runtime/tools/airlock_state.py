"""Durable state shared by the controller and machine coordinator."""

import json
import fcntl
import logging
import os
from pathlib import Path
import stat
import secrets
import tempfile
import time

LOGGER = logging.getLogger(__name__)


def atomic_json(path, value, *, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), mode)
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
            descriptor = Path(f"/proc/{owner['pid']}/fd/{owner['lock_fd']}")
            current = stream.fileno()
            holder = descriptor.stat()
            actual = os.fstat(current)
            if (holder.st_dev, holder.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("owner descriptor no longer refers to this lock")
            information = Path(f"/proc/{owner['pid']}/fdinfo/{owner['lock_fd']}").read_text()
            fields = dict(line.split(":", 1) for line in information.splitlines() if ":" in line)
            if (int(fields["pos"].strip()) != owner["lock_position"]
                    or not any("FLOCK" in line and "WRITE" in line
                               for line in information.splitlines() if line.startswith("lock:"))):
                raise ValueError("owner details belong to a different lock acquisition")
            elapsed = max(0, time.monotonic() - owner["acquired_monotonic"])
            return {**owner, "held": True, "verified": True, "elapsed_seconds": round(elapsed, 1)}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return {"held": True, "verified": False, "owner": "Unknown operation",
                    "reason": "Current owner details could not be verified"}


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
            # A kernel-held descriptor position identifies this acquisition even
            # if every attempt to replace stale diagnostic metadata fails.
            self.info.update(lock_fd=self.stream.fileno(), lock_position=None)
            try:
                position = secrets.randbits(32) + 1
                os.lseek(self.stream.fileno(), position, os.SEEK_SET)
                self.info["lock_position"] = position
            except OSError as exc:
                LOGGER.warning("Cannot identify lock acquisition for %s: %s", self.path, exc)
            self._remove_metadata()
            self.info.update(pid=os.getpid(), acquired_at=time.time(),
                             acquired_monotonic=time.monotonic())
            try:
                self.info.update(_process_identity(os.getpid()))
            except (OSError, ValueError, IndexError) as exc:
                LOGGER.warning("Cannot identify lock owner for %s: %s", self.path, exc)
            self.update()
            return self
        except BaseException:
            self.stream.close()
            raise

    def update(self, **context):
        self.info.update(context)
        try:
            atomic_json(self.metadata, self.info, mode=0o600)
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("Cannot publish lock owner metadata for %s: %s", self.path, exc)

    def _remove_metadata(self):
        try:
            self.metadata.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("Cannot remove lock owner metadata for %s: %s", self.path, exc)

    def fileno(self):
        return self.stream.fileno()

    def __exit__(self, *_):
        try:
            self._remove_metadata()
        finally:
            self.stream.close()
