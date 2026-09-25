#!/usr/bin/env python3
"""Restricted dom0 receiver for controller releases and direct pool receipts."""

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

SCHEMA = "airlock.sys-ctrl-machine-release.v2"
STORE_PATH = re.compile(r"/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+")
HEX = re.compile(r"[0-9a-f]+")
MAX_HEADER = 16 * 1024 * 1024
STATE = Path("/var/lib/airlock/machine")


def receive(action):
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
    importer = subprocess.Popen(["nix-store", "--import"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL)
    try:
        transferred = 0
        with gzip.GzipFile(fileobj=sys.stdin.buffer, mode="rb") as compressed:
            while chunk := compressed.read(1024 * 1024):
                transferred += len(chunk)
                if transferred > available - reserve:
                    raise ValueError("release stream exceeds dom0 import space limit")
                importer.stdin.write(chunk)
        importer.stdin.close()
        if importer.wait():
            raise ValueError("Nix closure import failed")
    finally:
        if importer.poll() is None:
            importer.kill()
            importer.wait()
    manifest = json.loads((Path(release) / "release.json").read_text())
    if (manifest.get("schema") != "airlock.machine-release.v1"
            or manifest.get("guestTransport") != "direct-pool-v1"
            or manifest.get("revision") != revision
            or manifest.get("lockHash") != lock_hash
            or manifest.get("profile") != "usb-lab"):
        raise ValueError("imported release provenance differs from request")
    coordinator = manifest.get("coordinator")
    if not isinstance(coordinator, str) or not re.fullmatch(
        STORE_PATH.pattern + r"/bin/airlock-machine-deploy", coordinator
    ):
        raise ValueError("invalid release coordinator")
    bound = subprocess.run([coordinator, "machine-bind", release, "-"],
                           input=json.dumps(header.get("receipts")),
                           check=True, text=True, capture_output=True)
    candidate = json.loads(bound.stdout)["candidate"]
    plan = subprocess.run([coordinator, "plan", candidate], check=True,
                          text=True, capture_output=True)
    # Retain only host/transport objects. The pool owns guest retention.
    root = STATE / "roots" / ("pending-" + Path(candidate).name)
    root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["nix-store", "--add-root", str(root), "--realise", release],
                   check=True, stdout=subprocess.DEVNULL)
    result = {"schema": SCHEMA, "release": candidate, "source_release": release,
              "plan": json.loads(plan.stdout), "paths_transferred": len(missing),
              "nar_bytes": transferred}
    if action == "apply":
        unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
        subprocess.run(["systemd-run", "--unit=" + unit, "--collect",
                        "--property=Type=exec", coordinator, "apply", candidate], check=True, stdout=subprocess.DEVNULL)
        result["unit"] = unit
    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    args = parser.parse_args()
    try:
        receive(args.action)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, EOFError, KeyError, TypeError) as exc:
        print(f"airlock-ctrl-machine-receive: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
