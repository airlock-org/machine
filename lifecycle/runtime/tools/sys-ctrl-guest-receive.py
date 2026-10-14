#!/usr/bin/env python3
"""Bind a guest admission before starting activation outside the qrexec session."""

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path


def receive(action, engine):
    line = sys.stdin.buffer.readline(65537)
    if len(line) > 65536 or not line.endswith(b"\n"):
        raise ValueError("invalid guest admission request")
    receipt = json.loads(line)
    if not isinstance(receipt, dict):
        raise ValueError("invalid guest admission receipt")
    approval = receipt.pop("review_authorization", None)
    prepared = subprocess.run([engine, "guest-plan", "-"], input=json.dumps(receipt),
                              text=True, capture_output=True)
    if prepared.returncode:
        raise ValueError(f"dom0 guest plan failed: {prepared.stderr.strip()[:4096]}")
    result = json.loads(prepared.stdout)
    if Path("/etc/airlock/review-required").exists():
        subprocess.run([engine, "review-bind", approval or "", result["candidate"]],
                       check=True, text=True, capture_output=True)
    if action == "apply":
        unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
        subprocess.run(["systemd-run", "--unit=" + unit, "--collect", "--property=Type=exec",
                        engine, "apply", result["candidate"]], check=True, stdout=subprocess.DEVNULL)
        result["unit"] = unit
    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--engine", required=True)
    args = parser.parse_args()
    try:
        receive(args.action, args.engine)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f"airlock-ctrl-guest-receive: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
