#!/usr/bin/env python3
"""Restricted dom0 receiver for a controller-built whole-machine release."""

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

SCHEMA = "airlock.sys-ctrl-machine-release.v1"
STORE_PATH = re.compile(r"/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+")
HEX = re.compile(r"[0-9a-f]+")


def receive(action):
    line = sys.stdin.buffer.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
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
    nar_bytes = header.get("nar_bytes")
    if type(nar_bytes) is not int or nar_bytes < 0:
        raise ValueError("invalid closure size")
    stat = os.statvfs("/nix/store")
    available = stat.f_bavail * stat.f_frsize
    reserve = 2 * 1024**3
    if available < nar_bytes + reserve:
        raise ValueError("insufficient dom0 store space for release import")
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
            or manifest.get("revision") != revision
            or manifest.get("lockHash") != lock_hash
            or manifest.get("profile") != "usb-lab"):
        raise ValueError("imported release provenance differs from request")
    coordinator = manifest.get("coordinator")
    if not isinstance(coordinator, str) or not re.fullmatch(
        STORE_PATH.pattern + r"/bin/airlock-machine-deploy", coordinator
    ):
        raise ValueError("invalid release coordinator")
    plan = subprocess.run([coordinator, "plan", release], check=True,
                          text=True, capture_output=True)
    if action == "plan":
        print(plan.stdout, end="")
    else:
        unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
        subprocess.run(["systemd-run", "--unit=" + unit, "--collect",
                        "--property=Type=exec", coordinator, "apply", release], check=True)
        print(json.dumps({"schema": SCHEMA, "unit": unit, "release": release,
                          "plan": json.loads(plan.stdout)}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    args = parser.parse_args()
    try:
        receive(args.action)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, EOFError) as exc:
        print(f"airlock-ctrl-machine-receive: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
