"""Dom0-side deployment evidence capture."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


class DeploymentDiagnostics:
    """Bounded, dom0-owned observation; never starts or repairs services."""

    def __init__(self, directory, domains):
        self.directory = Path(directory) / "diagnostics"
        self.directory.mkdir(mode=0o700)
        self.domains = domains
        self.stop = threading.Event()
        self.thread = None
        self.serial = 0
        self.lock = threading.Lock()
        self.cursor = None
        self.services = None

    def event(self, kind, **fields):
        with self.lock:
            with (self.directory / "events.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"time": time.time(), "event": kind, **fields}) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())

    def capture(self, label, args):
        try:
            result = subprocess.run(args, capture_output=True, timeout=3)
            self.save(label, result.stdout[-65536:] + result.stderr[-65536:])
            if result.returncode:
                self.event("capture-error", label=label, returncode=result.returncode)
        except Exception as exc:
            self.event("capture-error", label=label, error=str(exc))

    def save(self, label, data):
        path = self.directory / label
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def journal_snapshot(self):
        journal = self.directory / "journal.log"
        if not journal.exists() or journal.stat().st_size < 32 * 1024**2:
            args = [
                "journalctl",
                "-b",
                "--no-pager",
                "--show-cursor",
                "-o",
                "short-precise",
            ]
            args += ["--after-cursor", self.cursor] if self.cursor else ["-n", "250"]
            with journal.open("ab") as stream:
                # timeout bounds a broken journal reader; output goes straight
                # to disk, not an unbounded in-memory pipe.
                subprocess.run(
                    args, stdout=stream, stderr=stream, timeout=3, check=True
                )
                stream.flush()
                os.fsync(stream.fileno())
            with journal.open("rb") as stream:
                stream.seek(max(0, journal.stat().st_size - 8192))
                cursors = re.findall(rb"-- cursor: (\S+)", stream.read())
                if cursors:
                    self.cursor = cursors[-1].decode()

    def snapshot(self):
        try:
            self.journal_snapshot()
        except Exception as exc:
            self.event("capture-error", label="journal", error=str(exc))
        self.capture(
            "services.txt",
            [
                "systemctl",
                "show",
                "qubesd.service",
                "airlock-gui-input-connect.service",
                "airlock-gui-input-keyboard.service",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "Result",
            ],
        )
        services = (self.directory / "services.txt").read_text(errors="replace")
        if services != self.services:
            self.event("services-observed", state=services)
            self.services = services
        for name in [
            "hypervisor",
            *["guest-" + n + suffix for n in self.domains for suffix in ("", "-dm")],
        ]:
            path = Path("/var/log/xen/console") / (name + ".log")
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, os.fstat(stream.fileno()).st_size - 65536))
                    self.save(name + "-tail.log", stream.read(65536))
                    # Flush the existing source too: a copied tail alone cannot
                    # preserve earlier console output after sudden power loss.
                    os.fsync(stream.fileno())
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.event("capture-error", label=name, error=str(exc))

    def observe(self):
        while not self.stop.is_set():
            try:
                self.snapshot()
            except Exception as exc:
                print(f"deployment diagnostics: {exc}", file=sys.stderr, flush=True)
            self.stop.wait(2)

    def start(self):
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=10)

    def execute(self, *args, **kwargs):
        self.serial += 1
        identity = f"command-{self.serial:04d}"
        started = time.monotonic()
        timeout = kwargs.pop("timeout", None)
        self.event("command-start", id=identity, argv=list(args), timeout=timeout)
        try:
            with (
                (self.directory / (identity + ".stdout")).open("w+b") as out,
                (self.directory / (identity + ".stderr")).open("w+b") as err,
            ):
                with subprocess.Popen(
                    args, stdout=out, stderr=err, **kwargs
                ) as process:
                    while True:
                        try:
                            result = process.wait(timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            os.fsync(out.fileno())
                            os.fsync(err.fileno())
                            if (
                                timeout is not None
                                and time.monotonic() - started >= timeout
                            ):
                                process.kill()
                                process.wait()
                                os.fsync(out.fileno())
                                os.fsync(err.fileno())
                                raise subprocess.TimeoutExpired(args, timeout)
                    os.fsync(out.fileno())
                    os.fsync(err.fileno())
                    out.seek(0)
                    output = out.read().decode(errors="replace")
                    if result:
                        raise subprocess.CalledProcessError(result, args, output=output)
            self.event(
                "command-finish",
                id=identity,
                returncode=result,
                elapsed=time.monotonic() - started,
            )
            return output.strip()
        except Exception as exc:
            self.event(
                "command-error",
                id=identity,
                error=str(exc),
                elapsed=time.monotonic() - started,
                timeout=isinstance(exc, subprocess.TimeoutExpired),
                returncode=getattr(exc, "returncode", None),
            )
            raise
