#!/usr/bin/env python3
"""Submit a committed guest candidate from sys-dev to sys-ctrl."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

SCHEMA = "airlock.sys-ctrl-guest-request.v1"
SOURCE_SCHEMA = "airlock.sys-dev-machine-source.v1"
TARGETS = {"sys-dev", "sys-gui", "sys-net", "sys-firewall", "usr-home", "usr-browser"}
REPO = Path("/home/user/dev/airlock-machine")
MACHINE_SCHEMA = "airlock.sys-ctrl-machine-release.v1"


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def identity(repo):
    if not (repo / ".git").exists() or not (repo / "flake.lock").is_file():
        raise ValueError("airlock-machine checkout is missing")
    if output("git", "-C", str(repo), "status", "--porcelain"):
        raise ValueError("commit or stash checkout changes before submission")
    return output("git", "-C", str(repo), "rev-parse", "HEAD"), hashlib.sha256(
        (repo / "flake.lock").read_bytes()
    ).hexdigest()


def request(action, target, repo):
    if action in {"plan", "apply"}:
        if target not in TARGETS:
            raise ValueError("unsupported guest deployment action or target")
    elif action in {"machine-build", "machine-plan", "machine-apply"}:
        if target is not None:
            raise ValueError("machine requests must not select a guest")
    else:
        raise ValueError("unsupported deployment action")
    revision, lock_hash = identity(repo)
    if output("git", "-C", str(repo), "submodule", "status", "--recursive"):
        raise ValueError("source bundles with submodules are not supported")
    with tempfile.TemporaryDirectory(prefix="airlock-ctrl-submit-") as name:
        directory = Path(name)
        bundle = directory / "repo.bundle"
        subprocess.run(
            ["git", "-C", str(repo), "bundle", "create", str(bundle), "HEAD", "--all"],
            check=True,
        )
        if identity(repo) != (revision, lock_hash):
            raise ValueError("checkout changed while creating source bundle")
        (directory / "manifest.json").write_text(
            json.dumps({"schema": SOURCE_SCHEMA, "revision": revision,
                        "lock_hash": lock_hash, "submodules": []}) + "\n"
        )
        archive = directory / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(bundle, arcname="repo.bundle")
            stream.add(directory / "manifest.json", arcname="manifest.json")
        header = {"schema": SCHEMA, "action": action, "target": target,
                  "revision": revision, "lock_hash": lock_hash,
                  "archive_bytes": archive.stat().st_size}
        proc = subprocess.Popen(
            ["qrexec-client-vm", "sys-ctrl", "airlock.Ctrl.GuestDeploy"],
            stdin=subprocess.PIPE,
        )
        try:
            proc.stdin.write(json.dumps(header).encode() + b"\n")
            with archive.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    proc.stdin.write(chunk)
            proc.stdin.close()
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def serve():
    if os.environ.get("QREXEC_REMOTE_DOMAIN") != "sys-dev":
        raise ValueError("only sys-dev may submit candidates")
    line = sys.stdin.buffer.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise ValueError("invalid request header")
    header = json.loads(line)
    if not isinstance(header, dict) or header.get("schema") != SCHEMA:
        raise ValueError("unsupported request schema")
    action, target = header.get("action"), header.get("target")
    revision, lock_hash = header.get("revision"), header.get("lock_hash")
    size = header.get("archive_bytes")
    if (action in {"plan", "apply"} and target not in TARGETS) or (
        action in {"machine-build", "machine-plan", "machine-apply"} and target is not None
    ):
        raise ValueError("invalid deployment target")
    if action not in {"plan", "apply", "machine-build", "machine-plan", "machine-apply"}:
        raise ValueError("unsupported deployment action")
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("invalid source revision")
    if not isinstance(lock_hash, str) or len(lock_hash) != 64 or any(c not in "0123456789abcdef" for c in lock_hash):
        raise ValueError("invalid lock hash")
    if type(size) is not int or not 0 < size <= 512 * 1024 * 1024:
        raise ValueError("invalid source archive size")
    with tempfile.TemporaryFile() as archive:
        remaining = size
        while remaining:
            chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("truncated source archive")
            archive.write(chunk)
            remaining -= len(chunk)
        archive.seek(0)
        subprocess.run(
            ["airlock-sys-dev-machine-source", "import", "--revision", revision,
             "--lock-hash", lock_hash], stdin=archive, check=True,
            stdout=subprocess.DEVNULL,
        )
    if identity(REPO) != (revision, lock_hash):
        raise ValueError("controller checkout differs from submitted source")
    if action.startswith("machine-"):
        release = build_machine_release(revision, lock_hash)
        if action == "machine-build":
            print(json.dumps({"schema": MACHINE_SCHEMA, "release": release,
                              "revision": revision, "lock_hash": lock_hash}))
        else:
            send_machine_release(action.removeprefix("machine-"), release, revision, lock_hash)
        return
    installable = f".#nixosConfigurations.airlock-domain-{target}.config.system.build.toplevel"
    receipt = json.loads(output("airlock-sys-dev-machine-build", "admit", "--offline", target, installable))
    if receipt.get("source_revision") != revision or receipt.get("lock_hash") != lock_hash or receipt.get("domain") != target:
        raise ValueError("admission receipt differs from submitted candidate")
    result = subprocess.run(
        ["qrexec-client-vm", "dom0", "airlock.Ctrl.Guest" + action.title()],
        input=json.dumps(receipt) + "\n", text=True, check=True, capture_output=True,
    )
    print(result.stdout, end="")


def build_machine_release(revision, lock_hash):
    release = output("airlock-sys-dev-machine-build", "build", "--offline",
                     ".#airlock-machine-release-usb-lab")
    physical = Path("/rw/airlock-builder/merged") / release.lstrip("/")
    manifest = json.loads((physical / "release.json").read_text())
    if (manifest.get("schema") != "airlock.machine-release.v1"
            or manifest.get("revision") != revision
            or manifest.get("lockHash") != lock_hash
            or manifest.get("profile") != "usb-lab"):
        raise ValueError("built release provenance differs from submitted source")
    if identity(REPO) != (revision, lock_hash):
        raise ValueError("controller checkout changed during release build")
    record = Path.home() / ".local/share/airlock/machine-deploy" / revision
    record.mkdir(parents=True, exist_ok=True)
    subprocess.run(["airlock-config-nix", "build", release,
                    "--out-link", str(record / "release")], check=True)
    (record / "build.json").write_text(json.dumps({
        "schema": MACHINE_SCHEMA, "release": release,
        "revision": revision, "lock_hash": lock_hash,
    }, sort_keys=True) + "\n")
    return release


def send_machine_release(action, release, revision, lock_hash):
    raw = json.loads(output("airlock-config-nix", "path-info", "--recursive", "--json", release))
    metadata = {entry["path"]: entry for entry in raw} if isinstance(raw, list) else raw
    paths = sorted(metadata)
    if release not in paths:
        raise ValueError("release is missing from closure metadata")
    header = {"schema": MACHINE_SCHEMA, "release": release, "revision": revision,
              "lock_hash": lock_hash, "profile": "usb-lab",
              "nar_bytes": sum(entry["narSize"] for entry in metadata.values())}
    exporter = subprocess.Popen(["airlock-config-nix-store", "--export", *paths],
                                stdout=subprocess.PIPE)
    receiver = subprocess.Popen(
        ["qrexec-client-vm", "dom0", "airlock.Ctrl.Machine" + action.title()],
        stdin=subprocess.PIPE,
    )
    try:
        receiver.stdin.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=receiver.stdin, mode="wb", compresslevel=1) as stream:
            while chunk := exporter.stdout.read(1024 * 1024):
                stream.write(chunk)
        exporter.stdout.close()
        receiver.stdin.close()
        if exporter.wait() or receiver.wait():
            raise ValueError("machine release transfer or dom0 validation failed")
    finally:
        for process in (exporter, receiver):
            if process.poll() is None:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        sub.add_parser(action).add_argument("target", choices=sorted(TARGETS))
    for action in ("machine-build", "machine-plan", "machine-apply"):
        sub.add_parser(action)
    sub.add_parser("serve")
    args = parser.parse_args()
    try:
        if args.action == "serve":
            serve()
            return 0
        return request(args.action, getattr(args, "target", None), REPO)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        print(f"airlock-sys-ctrl-guest-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
