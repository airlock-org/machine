#!/usr/bin/env python3
"""Submit a committed guest candidate from usr-assistant to sys-ctrl."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

SCHEMA = "airlock.sys-ctrl-guest-request.v1"
RENAME_SCHEMA = "airlock.sys-ctrl-assistant-rename-request.v1"
SOURCE_SCHEMA = "airlock.sys-agent-machine-source.v1"
TARGETS = {"usr-assistant", "sys-ctrl", "sys-gui", "sys-net", "sys-firewall", "usr-home"}
REPO = Path("/home/user/dev/airlock-machine")
MACHINE_SCHEMA = "airlock.sys-ctrl-machine-release.v2"
STATE = Path.home() / ".local/share/airlock/controller"
BUILDER_STORE = Path("/rw/airlock-builder/merged")
BUILD_RECORDS = Path.home() / ".local/share/airlock/machine-deploy"


def candidate_directory(revision, assistant_rename=False):
    directory = STATE / "candidates" / revision
    return directory / "assistant-rename" if assistant_rename else directory


def build_directory(revision, assistant_rename=False):
    directory = BUILD_RECORDS / revision
    return directory / "assistant-rename" if assistant_rename else directory


def rename_manifest_matches(manifest, assistant_rename):
    return not assistant_rename or (
        manifest.get("privateVolumeCopies") == {"usr-assistant": "sys-agent"}
        and manifest.get("retireDomains") == ["sys-agent"]
    )


def record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def request(action, target, repo, *, assistant_rename=False):
    if assistant_rename and action not in {"machine-build", "machine-plan", "machine-apply"}:
        raise ValueError("assistant rename requires a whole-machine request")
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
        header = {"schema": RENAME_SCHEMA if assistant_rename else SCHEMA, "action": action, "target": target,
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
    caller = os.environ.get("QREXEC_REMOTE_DOMAIN")
    if caller not in {"usr-assistant", "sys-agent"}:
        raise ValueError("only usr-assistant may submit candidates")
    line = sys.stdin.buffer.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise ValueError("invalid request header")
    header = json.loads(line)
    if not isinstance(header, dict) or header.get("schema") not in {SCHEMA, RENAME_SCHEMA}:
        raise ValueError("unsupported request schema")
    assistant_rename = header["schema"] == RENAME_SCHEMA
    if caller == "sys-agent" and not assistant_rename:
        raise ValueError("only usr-assistant may submit ordinary candidates")
    action, target = header.get("action"), header.get("target")
    revision, lock_hash = header.get("revision"), header.get("lock_hash")
    size = header.get("archive_bytes")
    if (action in {"plan", "apply"} and target not in TARGETS) or (
        action in {"machine-build", "machine-plan", "machine-apply"} and target is not None
    ):
        raise ValueError("invalid deployment target")
    if action not in {"plan", "apply", "machine-build", "machine-plan", "machine-apply"}:
        raise ValueError("unsupported deployment action")
    if assistant_rename and not action.startswith("machine-"):
        raise ValueError("assistant rename requires a whole-machine request")
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
        with tarfile.open(fileobj=archive, mode="r:gz") as source:
            members = source.getmembers()
            if (len(members) != 2 or {member.name for member in members} != {"manifest.json", "repo.bundle"}
                    or any(not member.isfile() or member.size > 2 * 1024**3 for member in members)):
                raise ValueError("invalid source bundle members")
        archive.seek(0)
        candidate = candidate_directory(revision, assistant_rename)
        candidate.mkdir(parents=True, exist_ok=True)
        with (candidate / "source.tar.gz.new").open("wb") as saved:
            while chunk := archive.read(1024 * 1024):
                saved.write(chunk)
            saved.flush()
            os.fsync(saved.fileno())
        (candidate / "source.tar.gz.new").replace(candidate / "source.tar.gz")
        record(candidate / "request.json", header)
        archive.seek(0)
        subprocess.run(
            ["airlock-usr-assistant-machine-source", "import", "--revision", revision,
             "--lock-hash", lock_hash], stdin=archive, check=True,
            stdout=subprocess.DEVNULL,
        )
    if identity(REPO) != (revision, lock_hash):
        raise ValueError("controller checkout differs from submitted source")
    if action.startswith("machine-"):
        options = {"assistant_rename": True} if assistant_rename else {}
        release = build_machine_release(revision, lock_hash, **options)
        if action == "machine-build":
            print(json.dumps({"schema": MACHINE_SCHEMA, "release": release,
                              "revision": revision, "lock_hash": lock_hash}))
        else:
            send_machine_release(action.removeprefix("machine-"), release, revision, lock_hash, **options)
        return
    configuration = "sys-gui-usb-lab" if target == "sys-gui" else target
    installable = f".#nixosConfigurations.airlock-domain-{configuration}.config.system.build.toplevel"
    receipt = json.loads(output("airlock-usr-assistant-machine-build", "admit", target, installable))
    if receipt.get("source_revision") != revision or receipt.get("lock_hash") != lock_hash or receipt.get("domain") != target:
        raise ValueError("admission receipt differs from submitted candidate")
    result = subprocess.run(
        ["qrexec-client-vm", "dom0", "airlock.Ctrl.Guest" + action.title()],
        input=json.dumps(receipt) + "\n", text=True, capture_output=True,
    )
    if result.returncode:
        raise ValueError(f"dom0 guest {action} failed: {result.stderr.strip()[:4096]}")
    record(STATE / "candidates" / revision / f"guest-{target}-{action}.json", json.loads(result.stdout))
    print(result.stdout, end="")


def build_machine_release(revision, lock_hash, *, assistant_rename=False):
    started = time.monotonic()
    build_record = build_directory(revision, assistant_rename) / "build.json"
    if build_record.exists():
        saved = json.loads(build_record.read_text())
        release = saved.get("release", "")
        physical = BUILDER_STORE / release.lstrip("/")
        if (saved.get("revision") == revision and saved.get("lock_hash") == lock_hash
                and (physical / "release.json").is_file()
                and (build_record.parent / "release").is_symlink()
                and os.readlink(build_record.parent / "release") == release):
            manifest = json.loads((physical / "release.json").read_text())
            if (manifest.get("schema") == "airlock.machine-release.v1"
                    and manifest.get("profile") == "usb-lab"
                    and manifest.get("revision") == revision and manifest.get("lockHash") == lock_hash
                    and identity(REPO) == (revision, lock_hash)
                    and rename_manifest_matches(manifest, assistant_rename)):
                saved.update(build_seconds=time.monotonic() - started, build_reused=True)
                record(build_record, saved)
                return release
    installable = ".#airlock-machine-release-usb-lab"
    if assistant_rename:
        installable += "-assistant-rename"
    release = output("airlock-usr-assistant-machine-build", "build", installable)
    physical = BUILDER_STORE / release.lstrip("/")
    manifest = json.loads((physical / "release.json").read_text())
    if (manifest.get("schema") != "airlock.machine-release.v1"
            or manifest.get("revision") != revision
            or manifest.get("lockHash") != lock_hash
            or manifest.get("profile") != "usb-lab"
            or not rename_manifest_matches(manifest, assistant_rename)):
        raise ValueError("built release provenance differs from submitted source")
    if identity(REPO) != (revision, lock_hash):
        raise ValueError("controller checkout changed during release build")
    directory = build_directory(revision, assistant_rename)
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["airlock-config-nix", "build", release,
                    "--out-link", str(directory / "release")], check=True)
    record(directory / "build.json", {
        "schema": MACHINE_SCHEMA, "release": release,
        "build_seconds": time.monotonic() - started,
        "revision": revision, "lock_hash": lock_hash,
    })
    return release


def machine_response(data, stage):
    if not data:
        raise ValueError(f"dom0 closed the machine {stage} response; inspect its qrexec log")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError(f"invalid dom0 machine {stage} response")
    if result.get("error"):
        raise ValueError(f"dom0 machine {stage} failed: {str(result['error'])[:4096]}")
    return result


def send_prepared(release, revision, lock_hash, saved, *, assistant_rename=False):
    header = {"schema": MACHINE_SCHEMA, "release": saved["source_release"],
              "revision": revision, "lock_hash": lock_hash, "profile": "usb-lab",
              "prepared": saved["prepared"]}
    result = subprocess.run(["qrexec-client-vm", "dom0", "airlock.Ctrl.MachineApply"],
                            input=json.dumps(header) + "\n", text=True, capture_output=True)
    payload = machine_response(result.stdout, "prepared apply")
    if result.returncode:
        raise ValueError("prepared machine apply failed")
    payload["build_seconds"] = 0
    record(candidate_directory(revision, assistant_rename) / "apply.json", payload)
    print(json.dumps(payload, sort_keys=True))


def machine_metadata(topology):
    """Query each unique store path once for the whole submission."""
    paths = set()
    for domain in topology["domains"].values():
        backend = domain["backend"]
        if backend["type"] != "nix-closure":
            continue
        source = BUILDER_STORE / backend["store"]["source"].lstrip("/")
        candidates = [source / "store-paths", source / "closure/store-paths"] if source.is_dir() else [source]
        closure = next((path for path in candidates if path.is_file()), None)
        if closure is None:
            raise ValueError("machine closure is missing store-paths")
        paths.update(line.strip() for line in closure.read_text().splitlines() if line.strip())
        paths.add(backend["system"]["source"])
    metadata = {}
    # Bound argv length for large machines, without querying duplicates.
    paths = sorted(paths)
    for offset in range(0, len(paths), 512):
        raw = json.loads(output("airlock-config-nix", "path-info", "--json", *paths[offset:offset+512]))
        metadata.update({entry["path"]: entry for entry in raw} if isinstance(raw, list) else raw)
    if set(metadata) != set(paths):
        raise ValueError("machine package metadata differs from requested paths")
    return metadata


def admit_machine_guests(topology, roots, revision, lock_hash, metadata_file, admission, *, assistant_rename=False):
    receipts = {}
    for name, domain in sorted(topology["domains"].items()):
        backend = domain["backend"]
        if backend["type"] != "nix-closure":
            continue
        receipt = json.loads(output(
            "airlock-pool-send", "--reuse-generation", "--candidate-admission", admission,
            "--path-info-file", str(metadata_file), "--domain", name,
            "--system", backend["system"]["source"],
            "--closure-source", backend["store"]["source"],
            "--root-source", backend["root"]["source"],
            "--kernel-name", backend["kernel"]["name"],
            "--source-revision", revision, "--lock-hash", lock_hash,
        ))
        if receipt.get("root_artifact") != roots[name]:
            raise ValueError("admitted root differs from built release")
        receipts[name] = receipt
        record(candidate_directory(revision, assistant_rename) / "admissions.json", receipts)
    return receipts


def send_machine_release(action, release, revision, lock_hash, *, assistant_rename=False):
    started = time.monotonic()
    physical = BUILDER_STORE / release.lstrip("/")
    topology = json.loads((physical / "topology.json").read_text())
    roots = json.loads((physical / "root-artifacts.json").read_text())
    candidate = candidate_directory(revision, assistant_rename)
    options = {"assistant_rename": True} if assistant_rename else {}
    plan_record = candidate / "plan.json"
    if action == "apply" and plan_record.exists():
        saved = json.loads(plan_record.read_text())
        if (saved.get("prepared") and saved.get("source_release") == os.readlink(physical / "transport")):
            send_prepared(release, revision, lock_hash, saved, **options)
            return
    transport = os.readlink(physical / "transport")
    authorization = send_machine_transport("prepare", transport, revision, lock_hash, {})
    expected_domains = set(topology["domains"])
    if (authorization.get("schema") != "airlock.machine-admission.v1"
            or authorization.get("release") != transport
            or authorization.get("activated") is not False
            or set(authorization.get("domains", [])) != expected_domains
            or not re.fullmatch(r"[0-9a-f]{64}", authorization.get("admission", ""))):
        raise ValueError("invalid candidate admission authorization")
    record(candidate / "admission-prepare.json", authorization)
    receipts = {}
    metadata_started = time.monotonic()
    metadata = machine_metadata(topology)
    metadata_seconds = time.monotonic() - metadata_started
    # Operation-scoped metadata is never reused across builder mutations.
    with tempfile.TemporaryDirectory(prefix="airlock-machine-metadata-") as metadata_dir:
        metadata_file = Path(metadata_dir) / "paths.json"
        metadata_file.write_text(json.dumps(metadata))
        receipts = admit_machine_guests(topology, roots, revision, lock_hash, metadata_file,
                                       authorization["admission"], **options)
    result = send_machine_transport(action, transport, revision, lock_hash, receipts)
    result["candidate_preparation"] = authorization
    for key in ("paths_transferred", "nar_bytes", "compressed_bytes"):
        result[key] += authorization[key]
    result["elapsed_seconds"] = time.monotonic() - started
    result.setdefault("timings", {})["machine_metadata_seconds"] = metadata_seconds
    build_record = build_directory(revision, assistant_rename) / "build.json"
    result["build_seconds"] = json.loads(build_record.read_text())["build_seconds"]
    result["guest_transfer"] = {
        name: {key: receipt.get(key) for key in
               ("generation_reused", "objects_total", "objects_reused", "objects_transferred", "payload_bytes_transferred",
                "root_artifact", "root_reused", "root_payload_bytes_transferred",
                "root_logical_bytes", "root_allocated_bytes", "timings")}
        for name, receipt in receipts.items()
    }
    result["root_inventory"] = {
        receipt["root_artifact"]["sha256"]: {
            "logical_bytes": receipt["root_logical_bytes"],
            "allocated_bytes": receipt["root_allocated_bytes"],
        } for receipt in receipts.values()
    }
    record(candidate / (action + ".json"), result)
    print(json.dumps(result, sort_keys=True))


def send_machine_transport(action, transport, revision, lock_hash, receipts):
    raw = json.loads(output("airlock-config-nix", "path-info", "--recursive", "--json", transport))
    metadata = {entry["path"]: entry for entry in raw} if isinstance(raw, list) else raw
    header = {"schema": MACHINE_SCHEMA, "release": transport, "revision": revision,
              "lock_hash": lock_hash, "profile": "usb-lab", "receipts": receipts,
              "paths": {path: entry["narSize"] for path, entry in metadata.items()}}
    receiver = subprocess.Popen(
        ["qrexec-client-vm", "dom0", "airlock.Ctrl.Machine" + action.title()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    exporter = None
    try:
        receiver.stdin.write(json.dumps(header).encode() + b"\n")
        receiver.stdin.flush()
        response = machine_response(receiver.stdout.readline(16 * 1024 * 1024 + 1), "inventory")
        missing = response.get("missing")
        if (response.get("schema") != MACHINE_SCHEMA or not isinstance(missing, list)
                or any(not isinstance(path, str) for path in missing)
                or len(set(missing)) != len(missing) or not set(missing) <= metadata.keys()):
            raise ValueError("invalid dom0 missing-object response")
        with gzip.GzipFile(fileobj=receiver.stdin, mode="wb", compresslevel=1) as stream:
            if missing:
                exporter = subprocess.Popen(["airlock-config-nix-store", "--export", *missing],
                                            stdout=subprocess.PIPE)
                while chunk := exporter.stdout.read(1024 * 1024):
                    stream.write(chunk)
                exporter.stdout.close()
                if exporter.wait():
                    raise ValueError("machine release export failed")
            else:
                # Nix export streams terminate with a zero 64-bit word.
                stream.write(bytes(8))
        receiver.stdin.close()
        result = machine_response(receiver.stdout.read(), "planning")
        if receiver.wait():
            raise ValueError("machine release transfer or dom0 validation failed")
        return result
    finally:
        for process in (exporter, receiver):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


def observe():
    result = subprocess.run(
        ["qrexec-client-vm", "dom0", "airlock.Ctrl.MachineControl"],
        input=json.dumps({"schema": "airlock.machine-control.v1", "action": "status"}) + "\n",
        text=True, capture_output=True, check=True,
    )
    status = json.loads(result.stdout)
    record(STATE / "observed.json", status)
    revision = status.get("revision")
    if isinstance(revision, str) and len(revision) == 40 and all(c in "0123456789abcdef" for c in revision):
        record(STATE / "candidates" / revision / "outcome.json", status)
    # A submitted detached worker is not a successful deployment. Only dom0's
    # observed complete state updates the controller's current-release record.
    if status.get("phase") == "complete" and status.get("host_matches_selection") is True:
        record(STATE / "current.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        guest = sub.add_parser(action)
        guest.add_argument("target", choices=sorted(TARGETS))
        guest.add_argument("--repo", type=Path, default=Path.home() / "airlock-machine")
    for action in ("machine-build", "machine-plan", "machine-apply"):
        machine = sub.add_parser(action)
        machine.add_argument("--repo", type=Path, default=Path.home() / "airlock-machine")
        machine.add_argument("--assistant-rename", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("observe")
    args = parser.parse_args()
    try:
        if args.action == "serve":
            STATE.mkdir(parents=True, exist_ok=True)
            with (STATE / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                abandoned_sources = list((STATE / "candidates").glob("*/source.tar.gz.new"))
                abandoned_sources += list((STATE / "candidates").glob("*/assistant-rename/source.tar.gz.new"))
                for abandoned in abandoned_sources:
                    abandoned.unlink()
                serve()
            return 0
        if args.action == "observe":
            observe()
            return 0
        options = {"assistant_rename": True} if getattr(args, "assistant_rename", False) else {}
        return request(args.action, getattr(args, "target", None), getattr(args, "repo", REPO), **options)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        print(f"airlock-sys-ctrl-guest-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
