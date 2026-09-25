#!/usr/bin/env python3
"""Typed dom0 observation/recovery commands, including controller outage recovery."""

import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

SCHEMA = "airlock.machine-control.v1"
READ_ACTIONS = {"status", "verify", "trash-list", "release-list", "release-prune-plan"}
ACTIONS = READ_ACTIONS | {"resume", "rollback", "release-prune", "release-retain", "trash-purge"}


def arguments(request):
    if not isinstance(request, dict) or request.get("schema") != SCHEMA:
        raise ValueError("unsupported machine control schema")
    action = request.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unsupported machine control action")
    allowed = {"schema", "action"}
    args = [action]
    if action in {"release-prune-plan", "release-prune", "release-retain"}:
        allowed.add("release")
        release = request.get("release")
        if not isinstance(release, str):
            raise ValueError("missing release")
        path = Path(release)
        if not path.is_absolute() or str(path.resolve()) != release or not (
            path.parent == Path("/nix/store")
            or path.parent == Path("/var/lib/airlock/machine/candidates")
            or path == Path("/var/lib/airlock/machine/baseline")
        ):
            raise ValueError("release must be an exact recorded release path")
        args.append(release)
    if action == "trash-purge":
        allowed |= {"identity", "before_expiry"}
        identity = request.get("identity")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", identity):
            raise ValueError("invalid trash identity")
        args.append(identity)
        if type(request.get("before_expiry", False)) is not bool:
            raise ValueError("invalid expiry override")
        if request.get("before_expiry"):
            args.append("--before-expiry")
    if action in {"release-prune", "trash-purge"}:
        allowed.add("confirm")
        if request.get("confirm") != request.get("release" if action == "release-prune" else "identity"):
            raise ValueError("confirmation must equal the exact target")
        args += ["--confirm", request["confirm"]]
    if action == "release-retain":
        allowed.add("retain")
        if request.get("retain") not in ("true", "false"):
            raise ValueError("invalid retain value")
        args += ["--retain", request["retain"]]
    if action in {"release-prune", "release-prune-plan"}:
        allowed.add("allow_unexpired_trash")
        overrides = request.get("allow_unexpired_trash", [])
        if not isinstance(overrides, list) or len(overrides) > 100 or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
            for value in overrides
        ):
            raise ValueError("invalid early purge identities")
        for identity in overrides:
            args += ["--allow-unexpired-trash", identity]
    if set(request) - allowed:
        raise ValueError("unexpected control fields")
    return action, args


def main():
    try:
        data = sys.stdin.buffer.readline(65537)
        if len(data) > 65536 or not data.endswith(b"\n"):
            raise ValueError("invalid control request")
        action, args = arguments(json.loads(data))
        deployed = Path("/var/lib/airlock/machine/controller")
        engine = str(deployed) if deployed.exists() or deployed.is_symlink() else "/run/current-system/sw/bin/airlock-machine-deploy"
        if action in READ_ACTIONS or action == "release-retain":
            subprocess.run([engine, *args], check=True)
        else:
            unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
            subprocess.run(["systemd-run", "--unit=" + unit, "--collect",
                            "--property=Type=exec", engine, *args], check=True, stdout=subprocess.DEVNULL)
            print(json.dumps({"schema": SCHEMA, "unit": unit, "action": action}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"airlock-machine-control: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
