#!/usr/bin/env python3
"""Restricted dom0 receiver for controller releases and direct pool receipts."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airlock_state import atomic_json

SCHEMA = "airlock.sys-ctrl-machine-release.v2"
STORE_PATH = re.compile(r"/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+")
HEX = re.compile(r"[0-9a-f]+")
MAX_HEADER = 16 * 1024 * 1024
STATE = Path("/var/lib/airlock/machine")
ENGINE = None
REVIEW_REQUIRED = Path("/etc/airlock/review-required")


def check_review(header):
    if not REVIEW_REQUIRED.exists():
        return
    request = {"op": "check", "request_id": header.get("review_authorization"),
               "revision": header["revision"], "lock_hash": header["lock_hash"]}
    result = subprocess.run([ENGINE, "review-control"], input=json.dumps(request) + "\n",
                            check=True, text=True, capture_output=True)
    if json.loads(result.stdout)["binding"]["family"] != "machine":
        raise ValueError("whole-machine operation requires whole-machine approval")


def bind_review(header, candidate):
    if REVIEW_REQUIRED.exists():
        subprocess.run([ENGINE, "review-bind", header.get("review_authorization", ""), candidate],
                       check=True, text=True, capture_output=True)


class CountingReader:
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.count += len(data)
        return data


def prepared_record(candidate, coordinator, release, revision, lock_hash):
    checked = subprocess.run([coordinator, "prepared-check", candidate], check=True,
                             text=True, capture_output=True)
    result = json.loads(checked.stdout)
    payload = {"candidate": candidate, "coordinator": coordinator, "release": release,
               "revision": revision, "lock_hash": lock_hash, **result}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    directory = STATE / "prepared"
    target = directory / (token + ".json")
    atomic_json(target, payload, mode=0o600)
    return token, payload


def apply_prepared(header):
    check_review(header)
    token = header.get("prepared")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid prepared candidate token")
    saved = json.loads((STATE / "prepared" / (token + ".json")).read_text())
    if hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest() != token:
        raise ValueError("prepared candidate record checksum mismatch")
    if any(saved[key] != header[key] for key in ("release", "revision", "lock_hash")):
        raise ValueError("prepared candidate identity mismatch")
    coordinator = ENGINE if REVIEW_REQUIRED.exists() else saved["coordinator"]
    started = time.monotonic()
    checked = json.loads(subprocess.run(
        [coordinator, "prepared-check", saved["candidate"]],
        check=True, text=True, capture_output=True).stdout)
    if checked != {key: saved[key] for key in ("plan", "state")}:
        raise ValueError("prepared candidate is stale; run machine-plan again")
    bind_review(header, saved["candidate"])
    unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
    subprocess.run(["systemd-run", "--unit=" + unit, "--collect", "--property=Type=exec",
                    coordinator, "apply", saved["candidate"],
                    "--expected-state", saved["state"]], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({"schema": SCHEMA, "release": saved["candidate"],
                      "source_release": saved["release"], "plan": saved["plan"],
                      "prepared": token, "prepared_reused": True, "unit": unit,
                      "paths_transferred": 0, "nar_bytes": 0, "compressed_bytes": 0,
                      "timings": {"prepared_check_seconds": time.monotonic() - started}}), flush=True)


def receive(action):
    started = time.monotonic()
    line = sys.stdin.buffer.readline(MAX_HEADER + 1)
    if len(line) > MAX_HEADER or not line.endswith(b"\n"):
        raise ValueError("invalid release header")
    header = json.loads(line)
    if not isinstance(header, dict) or header.get("schema") != SCHEMA:
        raise ValueError("unsupported release schema")
    release = header.get("release")
    revision, lock_hash = header.get("revision"), header.get("lock_hash")
    if not isinstance(release, str) or not STORE_PATH.fullmatch(release):
        raise ValueError("invalid release store path")
    if not isinstance(revision, str) or len(revision) != 40 or not HEX.fullmatch(revision):
        raise ValueError("invalid source revision")
    if not isinstance(lock_hash, str) or len(lock_hash) != 64 or not HEX.fullmatch(lock_hash):
        raise ValueError("invalid lock hash")
    if header.get("profile") != "usb-lab":
        raise ValueError("unsupported machine profile")
    check_review(header)
    if "prepared" in header:
        if action != "apply":
            raise ValueError("prepared tokens are only accepted for apply")
        apply_prepared(header)
        return
    paths = header.get("paths")
    if not isinstance(paths, dict) or release not in paths or len(paths) > 100000:
        raise ValueError("invalid release inventory")
    if any(not STORE_PATH.fullmatch(path) or type(size) is not int or size < 0
           for path, size in paths.items()):
        raise ValueError("invalid release object")
    valid = set(subprocess.check_output(
        ["nix", "--extra-experimental-features", "nix-command", "path-info", "--all"],
        text=True,
    ).splitlines())
    missing = sorted(set(paths) - valid)
    stat = os.statvfs("/nix/store")
    available = stat.f_bavail * stat.f_frsize
    reserve = 2 * 1024**3
    if available < sum(paths[path] for path in missing) + reserve:
        raise ValueError("insufficient dom0 store space for release import")
    print(json.dumps({"schema": SCHEMA, "missing": missing}), flush=True)
    inventory_seconds = time.monotonic() - started
    started = time.monotonic()
    wire = CountingReader(sys.stdin.buffer)
    importer = subprocess.Popen(["nix-store", "--import"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL)
    try:
        transferred = 0
        read_seconds = 0.0
        import_feed_seconds = 0.0
        with gzip.GzipFile(fileobj=wire, mode="rb") as compressed:
            while True:
                step = time.monotonic()
                chunk = compressed.read(1024 * 1024)
                read_seconds += time.monotonic() - step
                if not chunk:
                    break
                transferred += len(chunk)
                if transferred > available - reserve:
                    raise ValueError("release stream exceeds dom0 import space limit")
                step = time.monotonic()
                importer.stdin.write(chunk)
                import_feed_seconds += time.monotonic() - step
        step = time.monotonic()
        importer.stdin.close()
        if importer.wait():
            raise ValueError("Nix closure import failed")
        import_finish_seconds = time.monotonic() - step
    finally:
        if importer.poll() is None:
            importer.kill()
            importer.wait()
    import_seconds = time.monotonic() - started
    started = time.monotonic()
    manifest = json.loads((Path(release) / "release.json").read_text())
    if (manifest.get("schema") != "airlock.machine-release.v1"
            or manifest.get("guestTransport") != "direct-pool-v1"
            or manifest.get("revision") != revision
            or manifest.get("lockHash") != lock_hash
            or manifest.get("profile") != "usb-lab"):
        raise ValueError("imported release provenance differs from request")
    if action == "prepare":
        if ENGINE is None:
            raise ValueError("candidate preparation requires the installed coordinator")
        prepared = subprocess.run([ENGINE, "admission-prepare", release, "--caller", "sys-ctrl"],
                                  check=True, text=True, capture_output=True)
        result = json.loads(prepared.stdout)
        result.update(paths_transferred=len(missing), nar_bytes=transferred,
                      compressed_bytes=wire.count,
                      timings={"inventory_validation_seconds": inventory_seconds,
                               "transfer_decompress_import_seconds": import_seconds})
        print(json.dumps(result, sort_keys=True), flush=True)
        return
    coordinator = manifest.get("coordinator")
    if not isinstance(coordinator, str) or not re.fullmatch(
        STORE_PATH.pattern + r"/bin/airlock-machine-deploy", coordinator
    ):
        raise ValueError("invalid release coordinator")
    if REVIEW_REQUIRED.exists():
        coordinator = ENGINE
    bound = subprocess.run([coordinator, "machine-bind", release, "-"],
                           input=json.dumps(header.get("receipts")),
                           check=True, text=True, capture_output=True)
    binding = json.loads(bound.stdout)
    candidate = binding["candidate"]
    binding_seconds = time.monotonic() - started
    started = time.monotonic()
    plan = subprocess.run([coordinator, "plan", candidate], check=True,
                          text=True, capture_output=True)
    bind_review(header, candidate)
    # Retain only host/transport objects. The pool owns guest retention.
    root = STATE / "roots" / ("pending-" + Path(candidate).name)
    root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["nix-store", "--add-root", str(root), "--realise", release],
                   check=True, stdout=subprocess.DEVNULL)
    result = {"schema": SCHEMA, "release": candidate, "source_release": release,
              "plan": json.loads(plan.stdout), "paths_transferred": len(missing),
              "nar_bytes": transferred, "compressed_bytes": wire.count,
              "timings": {"inventory_validation_seconds": inventory_seconds,
                          "transfer_decompress_import_seconds": import_seconds,
                          "transfer_read_and_decompress_seconds": read_seconds,
                          "import_pipe_write_seconds": import_feed_seconds,
                          "import_finish_seconds": import_finish_seconds,
                          "binding_validation_seconds": binding_seconds,
                          "plan_and_pin_seconds": time.monotonic() - started}}
    if action == "plan":
        prepare_started = time.monotonic()
        token, prepared = prepared_record(candidate, coordinator, release, revision, lock_hash)
        result["prepared"] = token
        result["plan"] = prepared["plan"]
        result["timings"]["prepared_record_seconds"] = time.monotonic() - prepare_started
    result["timings"].update(binding.get("timings", {}))
    if action == "apply":
        unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
        subprocess.run(["systemd-run", "--unit=" + unit, "--collect",
                        "--property=Type=exec", coordinator, "apply", candidate], check=True, stdout=subprocess.DEVNULL)
        result["unit"] = unit
    print(json.dumps(result, sort_keys=True))


def main():
    global ENGINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "plan", "apply"))
    parser.add_argument("--engine")
    args = parser.parse_args()
    ENGINE = args.engine
    try:
        receive(args.action)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, EOFError, KeyError, TypeError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += ": " + str(exc.stderr).strip()
        detail = detail[:4096]
        # Dom0 qrexec logs stderr locally. Return the bounded failure over the
        # protocol too, so the controller does not report an empty JSON reply.
        print(json.dumps({"schema": SCHEMA, "error": detail}), flush=True)
        print(f"airlock-ctrl-machine-receive: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
