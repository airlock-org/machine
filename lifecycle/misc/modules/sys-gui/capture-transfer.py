"""Event-triggered GNOME capture delivery over narrow Qubes RPCs."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

SCHEMA = "airlock.capture.v1"
KINDS = {"image": ("PICTURES", "Screenshots", {".png"}),
         "video": ("VIDEOS", "Screencasts", {".webm", ".mp4"})}
CHUNK = 1024 * 1024


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "airlock/captures"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(name):
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def capture_directory(kind):
    variable, folder, _ = KINDS[kind]
    result = subprocess.run(["@xdgUserDir@", variable], capture_output=True,
                            text=True, check=True)
    return Path(result.stdout.strip()) / folder


def capture_id(path, info):
    return hashlib.sha256(f"{path}\0{info.st_size}\0{info.st_mtime_ns}".encode()).hexdigest()


def capture_source(kind, filename):
    path = Path(filename).absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in KINDS[kind][2]:
        raise ValueError("Expected a regular GNOME capture file")
    if path.parent.resolve() != capture_directory(kind).resolve():
        raise ValueError("Capture must be in GNOME's capture directory")
    return path, info


def enqueue(kind, filename):
    path, info = capture_source(kind, filename)
    identity = capture_id(path, info)
    job = state_root() / "pending" / f"{identity}.json"
    if not (state_root() / "sent" / f"{identity}.json").exists():
        write_json(job, {"id": identity, "kind": kind, "source": str(path),
                         "size": info.st_size, "mtime": info.st_mtime_ns})
    return identity


def notify(message):
    print(message, file=sys.stderr)
    subprocess.run(["@notifySend@", "Screen capture", message], check=False)


def deliver(job, target):
    path = Path(job["source"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or capture_id(path, info) != job["id"]:
            raise ValueError("Capture changed after completion; keeping it locally")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        source.seek(0)
        header = {"schema": SCHEMA, "id": job["id"], "name": path.name,
                  "size": info.st_size, "sha256": digest}
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["@qrexec@", target, f"airlock.CaptureImport+{job['kind']}"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            try:
                process.stdin.write((json.dumps(header) + "\n").encode())
                shutil.copyfileobj(source, process.stdin, CHUNK)
                process.stdin.close()
                process.stdin = None
                output, _ = process.communicate(timeout=300)
                errors.seek(0)
                if process.returncode:
                    raise RuntimeError(errors.read(4096).decode(errors="replace") or "Capture transfer failed")
                receipt = json.loads(output)
                if receipt.get("id") != job["id"] or not receipt.get("path"):
                    raise ValueError("Capture receiver did not acknowledge delivery")
                receipt["kind"] = job["kind"]
                return receipt
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()


def drain(target, report=True):
    failed = False
    with locked("sender.lock"):
        for pending in sorted((state_root() / "pending").glob("*.json")):
            try:
                job = read_json(pending)
                sent = state_root() / "sent" / pending.name
                if not sent.exists():
                    write_json(sent, deliver(job, target))
                pending.unlink()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failed = True
                print(f"{pending.name}: {error}", file=sys.stderr)
    if failed and report:
        notify(f"Some captures could not be delivered to {target}. They remain in sys-gui; retry at next login or capture.")
    return not failed


def backfill():
    marker = state_root() / "backfill.json"
    if marker.exists():
        return
    for kind in KINDS:
        for path in sorted(capture_directory(kind).glob("*")):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in KINDS[kind][2]:
                continue
            if kind == "video":
                probe = subprocess.run(["@ffprobe@", "-v", "error", "-show_entries",
                                        "format=duration", "-of", "json", str(path)],
                                       capture_output=True, timeout=30)
                if probe.returncode:
                    continue
            enqueue(kind, str(path))
    write_json(marker, {"complete": True})


def request_header(stream):
    line = stream.readline(8193)
    if len(line) > 8192 or not line.endswith(b"\n"):
        raise ValueError("Invalid capture header")
    header = json.loads(line)
    if not isinstance(header, dict):
        raise ValueError("Expected capture metadata")
    return header


def validate_header(header, kind):
    if header.get("schema") != SCHEMA:
        raise ValueError("Unsupported capture protocol")
    for key in ("id", "sha256"):
        if not isinstance(header.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", header[key]):
            raise ValueError("Invalid capture identity")
    name = header.get("name")
    if not isinstance(name, str) or not name or len(name.encode()) > 240 or name in {".", ".."}:
        raise ValueError("Invalid capture filename")
    if any(c in name for c in "/\\") or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid capture filename")
    if Path(name).suffix.lower() not in KINDS[kind][2]:
        raise ValueError("Unexpected capture format")
    if type(header.get("size")) is not int or not 0 < header["size"] <= 1024 ** 4:
        raise ValueError("Invalid capture size")


def receive(kind, stream):
    header = request_header(stream)
    validate_header(header, kind)
    directory = capture_directory(kind)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".airlock-capture-", dir=directory)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output:
            remaining = header["size"]
            while remaining:
                data = stream.read(min(remaining, CHUNK))
                if not data:
                    raise ValueError("Incomplete capture")
                output.write(data)
                digest.update(data)
                remaining -= len(data)
            if stream.read(1):
                raise ValueError("Unexpected data after capture")
            if digest.hexdigest() != header["sha256"]:
                raise ValueError("Capture checksum mismatch")
            output.flush()
            os.fsync(output.fileno())
        with locked("receiver.lock"):
            receipt_path = state_root() / "imported" / f"{header['id']}.json"
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                if receipt["sha256"] != header["sha256"] or receipt["kind"] != kind:
                    raise ValueError("Capture identity conflict")
                # A lost acknowledgement must not duplicate an already imported capture.
                try:
                    existing_fd = os.open(receipt["path"], os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(existing_fd, "rb") as existing:
                        if (stat.S_ISREG(os.fstat(existing.fileno()).st_mode)
                                and hashlib.file_digest(existing, "sha256").hexdigest() == header["sha256"]):
                            return receipt
                except OSError:
                    pass
            original = Path(header["name"])
            destination = directory / original.name
            suffix = 0
            while True:
                # Persist the reservation first: a crash after publication but
                # before acknowledgement must still be deduplicated on retry.
                receipt = {"id": header["id"], "path": str(destination),
                           "sha256": header["sha256"], "kind": kind}
                write_json(receipt_path, receipt)
                try:
                    os.link(temporary, destination)
                    sync_directory(directory)
                    break
                except FileExistsError:
                    suffix += 1
                    destination = directory / f"{original.stem}-{header['id'][:12]}-{suffix}{original.suffix}"
            return receipt
    finally:
        Path(temporary).unlink(missing_ok=True)


def open_received(kind, stream):
    header = request_header(stream)
    identity = header.get("id", "")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid capture identity")
    action = header.get("action")
    if action not in {"open", "show"}:
        raise ValueError("Unknown capture action")
    receipt = read_json(state_root() / "imported" / f"{identity}.json")
    path = Path(receipt["path"])
    if receipt["kind"] != kind or path.parent.resolve() != capture_directory(kind).resolve() or not path.is_file():
        raise ValueError("Capture is unavailable")
    if action == "show":
        command = ["@gdbus@", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                   "--object-path", "/org/freedesktop/FileManager1", "--method",
                   "org.freedesktop.FileManager1.ShowItems", repr([path.as_uri()]), ""]
    else:
        command = ["@gio@", "open", str(path)]
    subprocess.run(command, check=True, timeout=30, stdout=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["enqueue", "retry", "open", "show", "receive", "receive-open"])
    parser.add_argument("kind", nargs="?", choices=list(KINDS))
    parser.add_argument("filename", nargs="?")
    parser.add_argument("--target", default="usr-home")
    args = parser.parse_args()
    if args.operation.startswith("receive"):
        if os.environ.get("QREXEC_REMOTE_DOMAIN") != "sys-gui" or args.kind not in KINDS:
            raise ValueError("Capture services accept only sys-gui and a capture kind")
        if args.operation == "receive":
            print(json.dumps(receive(args.kind, sys.stdin.buffer)))
        else:
            open_received(args.kind, sys.stdin.buffer)
        return
    if args.operation == "retry":
        backfill()
        drain(args.target)
        return
    if args.kind is None or args.filename is None:
        parser.error("Capture kind and filename are required")
    if args.operation == "enqueue":
        identity = enqueue(args.kind, args.filename)
    else:
        path, info = capture_source(args.kind, args.filename)
        identity = capture_id(path, info)
    drain(args.target)
    if args.operation in {"open", "show"}:
        if not (state_root() / "sent" / f"{identity}.json").exists():
            notify("This capture has not been delivered. Completed captures remain in sys-gui for retry.")
            return
        subprocess.run(["@qrexec@", args.target, "airlock.AppSessionReady"],
                       check=True, timeout=45, stdout=subprocess.DEVNULL)
        subprocess.run(["@qrexec@", args.target, f"airlock.CaptureOpen+{args.kind}"],
                       input=(json.dumps({"id": identity, "action": args.operation}) + "\n").encode(),
                       check=True, timeout=45)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Capture handoff failed: {error}", file=sys.stderr)
        sys.exit(1)
