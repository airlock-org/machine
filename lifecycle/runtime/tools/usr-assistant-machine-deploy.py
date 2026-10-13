#!/usr/bin/env python3
"""Build and deploy a committed whole-machine release directly from usr-assistant."""

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import time
import uuid

DEFAULT_REPO = Path("/home/user/airlock-machine")
DEFAULT_STATE = Path("/home/user/.local/share/airlock/machine-deploy")
DEFAULT_STORE_ROOT = Path("/rw/airlock-builder/merged")
REMOTE_INVENTORY = "nix --extra-experimental-features nix-command path-info --all"


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def physical_store_path(path):
    """Resolve a canonical store path through usr-assistant's private overlay."""
    path = Path(path)
    try:
        relative = path.relative_to("/nix/store")
    except ValueError as exc:
        raise RuntimeError(f"not a canonical Nix store path: {path}") from exc
    root = Path(os.environ.get("AIRLOCK_CONFIG_NIX_STORE_ROOT", DEFAULT_STORE_ROOT))
    return root / "nix/store" / relative


class Dom0:
    """Root command channel over the existing development-only dom0 PTY."""

    def __init__(self):
        # The qrexec client intentionally configures its controlling terminal
        # before passing raw keystrokes to dom0. Give it a private local PTY;
        # the deployment client itself can continue to use framed pipes.
        self.process = subprocess.Popen(
            [
                "script",
                "--quiet",
                "--return",
                "--flush",
                "--command",
                "airlock-dom0-shell",
                "/dev/null",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.buffer = b""
        self.until([b"DEVELOPMENT: dom0 shell."], timeout=60)
        token = uuid.uuid4().hex
        self.send(f"stty -echo; printf '%s%s\\n' '{token[:16]}' '{token[16:]}'")
        self.until([token.encode()], timeout=30)
        prompt, ready = uuid.uuid4().hex, uuid.uuid4().hex
        script = f"printf '%s\\n' {ready}; exec bash --noprofile --norc"
        self.send(f"sudo -S -p {prompt} bash -c {shlex.quote(script)}")
        for attempt in range(4):
            match, _ = self.until([prompt.encode(), ready.encode()], timeout=60)
            if match == ready.encode():
                break
            if attempt == 3:
                raise RuntimeError("dom0 sudo authentication failed")
            password = getpass.getpass("dom0 sudo password: ")
            self.send(password)
            del password

    def send(self, line):
        self.process.stdin.write(line.encode() + b"\n")
        self.process.stdin.flush()

    def until(self, tokens, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for token in tokens:
                index = self.buffer.find(token)
                if index >= 0:
                    result = self.buffer[:index]
                    self.buffer = self.buffer[index + len(token) :]
                    return token, result
            ready, _, _ = select.select(
                [self.process.stdout],
                [],
                [],
                min(1, max(0, deadline - time.monotonic())),
            )
            if ready:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError(
                        "dom0 connection closed; reconnect and run status"
                    )
                self.buffer += data
        raise RuntimeError("dom0 control command timed out; reconnect and run status")

    def run(self, script, timeout=60):
        token = uuid.uuid4().hex
        payload = base64.b64encode(("set -euo pipefail; " + script).encode()).decode()
        wrapper = (
            "import subprocess,base64,json; "
            f"p=subprocess.run(base64.b64decode('{payload}'),shell=True,executable='/run/current-system/sw/bin/bash',capture_output=True); "
            f"print('{token}'+base64.b64encode(json.dumps([p.returncode,p.stdout.decode(),p.stderr.decode()]).encode()).decode()+'{token}')"
        )
        line = "python3 -c " + shlex.quote(wrapper)
        if len(line) > 3500:
            raise RuntimeError(
                "control message too large for PTY; use artifact transfer"
            )
        self.send(line)
        self.until([token.encode()], timeout)
        _, response = self.until([token.encode()], timeout)
        status, output, error = json.loads(base64.b64decode(response))
        if status:
            raise RuntimeError(f"dom0 command failed ({status}): {error or output}")
        return output.strip()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def source_identity(repo):
    if not (repo / ".git").exists() or not (repo / "flake.lock").is_file():
        raise RuntimeError(f"missing airlock-machine checkout: {repo}")
    if command("git", "-C", str(repo), "status", "--porcelain"):
        raise RuntimeError("commit or stash checkout changes before deployment")
    return (
        command("git", "-C", str(repo), "rev-parse", "HEAD"),
        hashlib.sha256((repo / "flake.lock").read_bytes()).hexdigest(),
    )


def validate_manifest(manifest, revision, lock_hash, profile):
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest is not a JSON object")
    if (
        manifest.get("schema") != "airlock.machine-release.v1"
        or manifest.get("revision") != revision
        or manifest.get("lockHash") != lock_hash
        or manifest.get("profile") != profile
    ):
        raise RuntimeError("release provenance differs from the committed checkout")
    coordinator = manifest.get("coordinator")
    if not isinstance(coordinator, str) or not coordinator.startswith("/nix/store/"):
        raise RuntimeError("release manifest has no valid coordinator")
    return coordinator


def build(profile, repo=DEFAULT_REPO, state=DEFAULT_STATE):
    repo, state = Path(repo), Path(state)
    revision, lock_hash = source_identity(repo)
    installable = f"{repo}#airlock-machine-release-{profile}"
    result = json.loads(
        command(
            "airlock-config-nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "build",
            installable,
            "--no-link",
            "--json",
        )
    )
    try:
        release = result[0]["outputs"]["out"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Nix did not return a release output") from exc
    if source_identity(repo) != (revision, lock_hash):
        raise RuntimeError("checkout changed during build; retry from a clean commit")
    manifest = json.loads((physical_store_path(release) / "release.json").read_text())
    validate_manifest(manifest, revision, lock_hash, profile)
    directory = state / revision
    directory.mkdir(parents=True, exist_ok=True)
    command(
        "airlock-config-nix",
        "build",
        release,
        "--out-link",
        str(directory / "release"),
    )
    receipt = {
        "schema": "airlock.sys-agent-machine-release.v1",
        "revision": revision,
        "lock_hash": lock_hash,
        "profile": profile,
        "release": release,
    }
    atomic_json(directory / "build.json", receipt)
    return receipt


def closure_metadata(release):
    raw = json.loads(
        command(
            "airlock-config-nix",
            "path-info",
            "--recursive",
            "--json",
            release,
        )
    )
    if isinstance(raw, list):
        return {entry["path"]: entry for entry in raw}
    return raw


def transfer(release, session, state=DEFAULT_STATE):
    metadata = closure_metadata(release)
    valid = set(session.run(REMOTE_INVENTORY, timeout=120).splitlines())
    missing = sorted(path for path in metadata if path not in valid)
    if not missing:
        return {"paths_total": len(metadata), "paths_transferred": 0, "nar_bytes": 0}
    needed = sum(metadata[path]["narSize"] for path in missing)
    free = int(
        session.run(
            "python3 -c 'import os; s=os.statvfs(\"/nix/store\"); print(s.f_bavail*s.f_frsize)'"
        )
    )
    required = needed + 2 * 1024**3
    if free < required:
        raise RuntimeError(
            f"dom0 needs approximately {required / 1024**3:.1f} GiB free to import this release; "
            f"only {free / 1024**3:.1f} GiB is available"
        )
    state = Path(state)
    incoming = state / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    fifo = incoming / f"{uuid.uuid4().hex}.nar.gz.fifo"
    os.mkfifo(fifo, mode=0o600)
    export = subprocess.Popen(
        ["airlock-config-nix-store", "--export", *missing],
        stdout=subprocess.PIPE,
    )
    # Open the FIFO only in the writer child. Its blocking open rendezvous with
    # qrexec's reader; an O_RDWR parent descriptor could let a tiny export
    # finish before the reader opens, discarding the buffered stream.
    gzip = subprocess.Popen(
        ["bash", "-c", 'exec gzip -1c > "$1"', "fifo-writer", str(fifo)],
        stdin=export.stdout,
    )
    export.stdout.close()
    fetch = shlex.join(["qrexec-client", "-d", "usr-assistant", "user:cat " + str(fifo)])
    print(f"Streaming {len(missing)} missing Nix paths to dom0…", flush=True)
    try:
        session.run(
            f"umask 077; bash -o pipefail -c {shlex.quote(fetch + ' | gzip -dc | nix-store --import > /var/lib/airlock/machine/import.log')}",
            timeout=3600,
        )
        if export.wait() or gzip.wait():
            raise RuntimeError("Nix closure export failed")
    finally:
        fifo.unlink(missing_ok=True)
        if gzip.poll() is None:
            gzip.terminate()
        if export.poll() is None:
            export.terminate()
        gzip.wait()
        export.wait()
    return {
        "paths_total": len(metadata),
        "paths_transferred": len(missing),
        "nar_bytes": needed,
    }


def deployed_engine(session):
    return session.run(
        "if [ -x /var/lib/airlock/machine/controller ]; then "
        "printf %s /var/lib/airlock/machine/controller; else "
        "printf %s /run/current-system/sw/bin/airlock-machine-deploy; fi"
    )


def run_detached(session, action):
    unit = "airlock-machine-deploy-" + uuid.uuid4().hex[:12]
    command_line = shlex.join(
        [
            "systemd-run",
            "--unit=" + unit,
            "--collect",
            "--property=Type=exec",
            *action,
        ]
    )
    session.run(command_line)
    return unit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "build",
            "guest-plan",
            "guest-apply",
            "plan",
            "apply",
            "status",
            "verify",
            "resume",
            "rollback",
            "trash-list",
            "trash-purge",
            "release-prune-plan",
            "release-list",
            "release-prune",
            "release-retain",
        ],
    )
    parser.add_argument("--profile", default="usb-lab", choices=["usb-lab"])
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--release")
    parser.add_argument("--target")
    parser.add_argument("--legacy-dom0", action="store_true", help="use the development-only direct dom0 transport")
    parser.add_argument("--assistant-rename", action="store_true",
                        help="submit the one-shot sys-agent private-volume migration through sys-ctrl")
    parser.add_argument("--identity")
    parser.add_argument("--confirm")
    parser.add_argument("--retain", choices=("true", "false"))
    parser.add_argument("--allow-unexpired-trash", action="append", default=[])
    parser.add_argument("--before-expiry", action="store_true")
    args = parser.parse_args(argv)
    if args.assistant_rename and (args.action not in {"build", "plan", "apply"} or args.legacy_dom0):
        parser.error("assistant rename requires controller build, plan, or apply")
    if args.action in ("guest-plan", "guest-apply"):
        if not args.target:
            parser.error("--target is required for guest deployment")
        return subprocess.call([
            "airlock-sys-ctrl-guest-deploy",
            args.action.removeprefix("guest-"),
            args.target,
            "--repo", str(args.repo),
        ])
    if args.action in ("build", "plan", "apply") and not args.legacy_dom0:
        if args.assistant_rename:
            source_identity(args.repo)
            # The installed old assistant client predates this option. Run the
            # frontend from the same clean, committed checkout being submitted.
            return subprocess.call([
                sys.executable, str(args.repo / "lifecycle/runtime/tools/sys-ctrl-guest-deploy.py"),
                "machine-" + args.action, "--repo", str(args.repo), "--assistant-rename",
            ])
        return subprocess.call(["airlock-sys-ctrl-guest-deploy", "machine-" + args.action,
                                "--repo", str(args.repo)])
    if (
        args.action in ("release-prune-plan", "release-prune", "release-retain")
        and not args.release
    ):
        parser.error("--release is required")
    if args.action == "release-retain" and args.retain is None:
        parser.error("--retain is required")
    if args.action == "trash-purge" and not args.identity:
        parser.error("--identity is required")
    if args.action in ("release-prune", "trash-purge"):
        target = args.release if args.action == "release-prune" else args.identity
        if args.confirm != target:
            parser.error("--confirm must match the exact target")
    if not args.legacy_dom0:
        request = {"schema": "airlock.machine-control.v1", "action": args.action}
        for key in ("release", "identity", "confirm", "retain"):
            value = getattr(args, key)
            if value is not None:
                request[key] = value
        if args.allow_unexpired_trash:
            request["allow_unexpired_trash"] = args.allow_unexpired_trash
        if args.before_expiry:
            request["before_expiry"] = True
        return subprocess.run(
            ["qrexec-client-vm", "dom0", "airlock.Ctrl.MachineControl"],
            input=json.dumps(request) + "\n", text=True,
        ).returncode
    session = None
    try:
        receipt = None
        if args.action in ("build", "plan", "apply"):
            receipt = build(args.profile, args.repo, args.state)
            print(json.dumps(receipt, indent=2), flush=True)
            if args.action == "build":
                return 0
        session = Dom0()
        if args.action in ("plan", "apply"):
            transfer(receipt["release"], session, args.state)
            manifest = json.loads(
                session.run("cat " + shlex.quote(receipt["release"] + "/release.json"))
            )
            engine = validate_manifest(
                manifest,
                receipt["revision"],
                receipt["lock_hash"],
                receipt["profile"],
            )
            preview = session.run(
                shlex.join([engine, "plan", receipt["release"]]), timeout=900
            )
            print(preview, flush=True)
            if args.action == "plan":
                return 0
            if (
                input(
                    "Apply this whole-machine release (may restart VMs)? [y/N] "
                ).lower()
                != "y"
            ):
                return 0
            action = [engine, "apply", receipt["release"]]
        else:
            action = [deployed_engine(session), args.action]
            if args.action in ("release-prune-plan", "release-prune", "release-retain"):
                action += [args.release]
            if args.action == "trash-purge":
                action += [args.identity]
            if args.action in ("release-prune", "trash-purge"):
                action += ["--confirm", args.confirm]
            if args.action in ("release-prune-plan", "release-prune"):
                for identity in args.allow_unexpired_trash:
                    action += ["--allow-unexpired-trash", identity]
            if args.action == "trash-purge" and args.before_expiry:
                action += ["--before-expiry"]
            if args.action == "release-retain":
                action += ["--retain", args.retain]
        if args.action in (
            "status",
            "verify",
            "trash-list",
            "release-prune-plan",
            "release-list",
            "release-retain",
        ):
            print(session.run(shlex.join(action), timeout=900))
        else:
            unit = run_detached(session, action)
            follow = (
                "release-prune-plan --release " + shlex.quote(args.release)
                if args.action == "release-prune"
                else "trash-list"
                if args.action == "trash-purge"
                else "status"
            )
            print(
                f"Started {unit} in dom0; use {follow} to inspect its record and the unit journal for errors."
            )
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"airlock-usr-assistant-machine-deploy: {exc}", file=sys.stderr)
        return 1
    finally:
        if session:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
