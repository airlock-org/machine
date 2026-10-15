#!/usr/bin/env python3
"""Bind a guest admission before starting activation outside the qrexec session."""

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

SCHEMA = "airlock.guest-deploy-error.v1"


class GuestFailure(ValueError):
    def __init__(self, result):
        self.returncode = result.returncode
        self.stdout = getattr(result, "stdout", "")
        self.stderr = getattr(result, "stderr", "")
        super().__init__(self.stderr or self.stdout or f"Guest operation failed (exit {self.returncode})")


def engine_result(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise GuestFailure(result)
    return result

def receive(action, engine):
    line = sys.stdin.buffer.readline(65537)
    if len(line) > 65536 or not line.endswith(b"\n"):
        raise ValueError("invalid guest admission request")
    receipt = json.loads(line)
    if not isinstance(receipt, dict):
        raise ValueError("invalid guest admission receipt")
    approval = receipt.pop("review_authorization", None)
    prepared = engine_result([engine, "guest-plan", "-"], input=json.dumps(receipt))
    result = json.loads(prepared.stdout)
    if Path("/etc/airlock/review-required").exists():
        engine_result([engine, "review-bind", approval or "", result["candidate"]])
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
        response = {}
        if isinstance(exc, (subprocess.CalledProcessError, GuestFailure)):
            try:
                response = json.loads(exc.stdout or "{}")
            except ValueError:
                pass
            if not isinstance(response, dict):
                response = {}
            detail = response.get("error") or exc.stderr or str(exc)
        else:
            detail = str(exc)
        detail = str(detail)[:4096]
        print(json.dumps({"schema": SCHEMA, "status": response.get("status", "failed"),
                          "error": detail, "lock": response.get("lock"),
                          "operation": "guest-" + args.action,
                          "exit_code": getattr(exc, "returncode", None)}), flush=True)
        print(f"airlock-ctrl-guest-receive: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
