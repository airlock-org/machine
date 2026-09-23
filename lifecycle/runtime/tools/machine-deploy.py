#!/usr/bin/env python3
"""Root-owned, journalled whole-machine Nix deployment coordinator.

Transport and configuration evaluation deliberately live outside this module.
Qubes remains responsible for VM lifecycle, devices, policy and storage.
"""

import argparse
from copy import deepcopy
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

STATE = Path("/var/lib/airlock/machine")
SCHEMA = "airlock.machine-release.v1"
ADMISSION_SCHEMA = "airlock.closure-store.admission.v1"
GENERATION_INFO_SCHEMA = "airlock.closure-store.generation-info.v1"
TRASH_SCHEMA = "airlock.machine-trash.v1"
DEFAULT_TRASH_RETENTION_DAYS = 30
RETIRED_POLICY = "/var/lib/airlock/qubes-policy.d/05-airlock-retired.policy"


class DeploymentError(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(*args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, **kwargs
    ).stdout.strip()


def load_release(path):
    path = Path(path).resolve()
    release = read_json(path / "release.json")
    if release.get("schema") != SCHEMA:
        raise DeploymentError("unsupported machine release schema")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", release["profile"]):
        raise DeploymentError("invalid machine profile")
    if not release["host"].startswith("/nix/store/"):
        raise DeploymentError("host must be a built Nix system")
    topology = read_json(path / "topology.json")
    if topology.get("schema") != "airlock.qubes-topology.v1":
        raise DeploymentError("unsupported topology schema")
    staged = release.get("stagedGenerations", {})
    if not isinstance(staged, dict) or any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        or not isinstance(generation, int)
        or generation < 1
        for name, generation in staged.items()
    ):
        raise DeploymentError("invalid staged generation map")
    retire = release.get("retireDomains", [])
    if (
        not isinstance(retire, list)
        or len(set(retire)) != len(retire)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            for name in retire
        )
    ):
        raise DeploymentError("invalid retired domain list")
    if set(retire) & set(topology["domains"]):
        raise DeploymentError("retired domains must be absent from the topology")
    retention = release.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS)
    if not isinstance(retention, int) or not 1 <= retention <= 3650:
        raise DeploymentError("trash retention must be between 1 and 3650 days")
    return release, topology


def active_profile():
    marker = Path("/etc/airlock/machine-profile")
    if marker.exists():
        return marker.read_text().strip()
    # Compatibility with images predating the deployment coordinator.
    for profile in ("usb-lab", "usb-gui-lab"):
        if Path("/etc/airlock", profile).exists():
            return profile
    raise DeploymentError("this boot profile does not support machine deployment")


def domain_references(topology):
    domains = topology["domains"]
    result = {}
    defaults = topology.get("prefs", {})
    for name, domain in domains.items():
        prefs = domain.get("prefs", {})
        refs = [domain.get("netvm"), domain.get("template")]
        refs += [
            prefs.get(key, defaults.get("default_" + key))
            for key in ("guivm", "audiovm", "dispvm")
        ]
        refs.append(prefs.get("default_dispvm", defaults.get("default_dispvm")))
        devices = domain.get("devices", {})
        if isinstance(devices, dict):
            for assignments in devices.values():
                refs += [device.get("backend") for device in assignments]
        result[name] = {ref for ref in refs if ref and ref != name}
    return result


def topology_references(topology):
    """Explicit machine-wide references that must be removed before retirement."""
    defaults = topology.get("prefs", {})
    vm_prefs = (
        "clockvm",
        "default_dispvm",
        "default_netvm",
        "default_guivm",
        "default_audiovm",
        "default_template",
        "management_dispvm",
        "updatevm",
    )
    refs = {
        f"prefs.{key}": defaults[key]
        for key in vm_prefs
        if isinstance(defaults.get(key), str)
    }
    for index, rule in enumerate(topology.get("qrexec_policy", {}).get("rules", [])):
        for endpoint in ("source", "target"):
            refs[f"qrexec_policy.rules[{index}].{endpoint}"] = rule.get(endpoint)
    return refs


def dependencies(topology):
    domains = topology["domains"]
    return {
        name: {ref for ref in refs if ref in domains}
        for name, refs in domain_references(topology).items()
    }


def ordered(topology):
    pending = dependencies(topology)
    result = []
    while pending:
        ready = sorted(name for name, deps in pending.items() if not deps - set(result))
        if not ready:
            raise DeploymentError("VM dependency cycle: " + ", ".join(sorted(pending)))
        result.extend(ready)
        for name in ready:
            del pending[name]
    return result


def plan(old, new, running, retire_removed=(), trashed=(), retain_removed=False):
    """Conservative lifecycle plan; never silently ignore destructive changes."""
    before, after = old["domains"], new["domains"]
    removed = before.keys() - after.keys()
    # A trashed VM is still registered with Qubes, so setup-config's ordinary
    # "domain exists" validation cannot distinguish it from an active target.
    unavailable = removed | (set(trashed) - after.keys())
    authorized = set(retire_removed)
    if authorized - removed:
        raise DeploymentError(
            "retirement does not match removed VMs: "
            + ", ".join(sorted(authorized - removed))
        )
    unauthorized = removed - authorized
    if unauthorized and not retain_removed:
        raise DeploymentError(
            "VM removal requires explicit retirement: "
            + ", ".join(sorted(unauthorized))
        )
    for name, refs in domain_references(new).items():
        blocked = refs & unavailable
        if blocked:
            raise DeploymentError(
                f"{name}: still depends on retired VM(s): " + ", ".join(sorted(blocked))
            )
    for field, target in topology_references(new).items():
        if target in unavailable:
            raise DeploymentError(f"{field}: still references retired VM {target}")
    for name, domain in after.items():
        backend = domain.get("backend", {})
        if backend.get("type") != "nix-closure":
            raise DeploymentError(
                f"{name}: only Nix-closure VM deployment is supported"
            )
        if backend.get("store", {}).get("mode") != "shared-closure":
            raise DeploymentError(f"{name}: shared-closure storage is required")
        if name not in before:
            continue
        previous = before[name]
        for key in ("class", "template", "devices"):
            if previous.get(key) != domain.get(key):
                raise DeploymentError(f"{name}: changing {key} requires a migration")
        for key in ("type", "state"):
            if previous.get("backend", {}).get(key) != backend.get(key):
                raise DeploymentError(
                    f"{name}: changing backend {key} requires a migration"
                )
        for key in ("prefs", "features", "services"):
            missing = previous.get(key, {}).keys() - domain.get(key, {}).keys()
            if missing:
                raise DeploymentError(
                    f"{name}: removing {key} requires explicit reset values"
                )
    # setup-config is a converger, not a garbage collector.
    for key in (("prefs",) if retain_removed else ("kernels", "templates", "prefs")):
        if old.get(key, {}).keys() - new.get(key, {}).keys():
            raise DeploymentError(f"removing topology {key} requires a migration")
    changed = {name for name in after if before.get(name) != after[name]}
    if old.get("kernels") != new.get("kernels") or old.get("prefs") != new.get("prefs"):
        changed.update(after)
    affected = set(changed) | removed
    deps = dependencies(old)
    while True:
        expanded = affected | {name for name, refs in deps.items() if refs & affected}
        if expanded == affected:
            break
        affected = expanded
    start = {
        name
        for name in after
        if name in running or after[name].get("prefs", {}).get("autostart") is True
    }
    new_deps = dependencies(new)
    while True:
        expanded = start | set().union(*(new_deps[name] for name in start))
        if expanded == start:
            break
        start = expanded
    return {
        "changed": sorted(changed),
        "stage": sorted(
            name
            for name in changed
            if before.get(name, {}).get("backend") != after[name].get("backend")
        ),
        "stop": [name for name in reversed(ordered(old)) if name in affected & running],
        "start": [name for name in ordered(new) if name in start],
        "affected": sorted(affected),
        "retire": sorted(removed),
        "restore": sorted(set(after) & set(trashed)),
        "retained_undeclared": sorted(removed),
    }


class Coordinator:
    def __init__(self, state=STATE, command=run):
        self.state = Path(state)
        self.command = command

    @contextlib.contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("another machine deployment is running") from exc
            yield lock

    def pointer(self):
        path = self.state / "selection.json"
        return read_json(path) if path.exists() else None

    def running(self):
        text = self.command("qvm-ls", "--raw-data", "--fields", "NAME,STATE")
        return {
            line.split("|")[0]
            for line in text.splitlines()
            if line.endswith("|Running") and not line.startswith("dom0|")
        }

    def pin(self, name, path):
        root = self.state / "roots" / name
        root.parent.mkdir(exist_ok=True)
        self.command("nix-store", "--add-root", str(root), "--realise", str(path))

    def trash_records(self):
        records = []
        directory = self.state / "trash"
        if not directory.exists():
            return records
        for path in sorted(directory.glob("*.json")):
            record = read_json(path)
            if record.get("schema") != TRASH_SCHEMA or record.get("id") != path.stem:
                raise DeploymentError(f"invalid machine trash record: {path.name}")
            records.append(record)
        return records

    def active_trash(self):
        active = {}
        for record in self.trash_records():
            if record.get("status") not in ("retiring", "trashed", "purging"):
                continue
            name = record["domain"]
            if name in active:
                raise DeploymentError(f"multiple active trash records for {name}")
            active[name] = record
        return active

    def trash_path(self, identity):
        if not re.fullmatch(r"[0-9]+-[A-Za-z0-9_.-]+", identity):
            raise DeploymentError("invalid trash record id")
        return self.state / "trash" / f"{identity}.json"

    def retirement_details(self, topology, names, retention_days):
        if not names:
            return []
        if not Path(RETIRED_POLICY).is_file():
            raise DeploymentError("retired-domain qrexec policy is not installed")
        details = []
        for name in names:
            domain = topology["domains"][name]
            generation_text = self.command(
                "qvm-features", name, "airlock-store-generation"
            )
            if not generation_text.isdigit():
                raise DeploymentError(
                    f"{name}: selected pool generation is unavailable"
                )
            generation = int(generation_text)
            info = self.generation_info(name, generation)
            if info.get("system") != domain["backend"]["system"]["source"]:
                raise DeploymentError(
                    f"{name}: selected pool generation differs from release"
                )
            details.append(
                {
                    "domain": name,
                    "uuid": self.command("qvm-prefs", name, "uuid"),
                    "private": domain.get("backend", {})
                    .get("state", {})
                    .get("persistent"),
                    "pool_generation": generation,
                    "pool_manifest": info.get("manifest"),
                    "retention_days": retention_days,
                }
            )
        return details

    def prepare_retirement(self, name, domain, release, transaction, retention_days):
        existing = self.active_trash().get(name)
        if existing:
            if existing.get("status") == "purging":
                raise DeploymentError(f"{name}: trash purge is already in progress")
            if existing.get("domain_config") != domain:
                raise DeploymentError(f"{name}: active trash identity differs")
            if self.command("qvm-prefs", name, "uuid") != existing.get("uuid"):
                raise DeploymentError(
                    f"{name}: registered VM identity differs from trash"
                )
            return existing
        generation_text = self.command("qvm-features", name, "airlock-store-generation")
        if not generation_text.isdigit():
            raise DeploymentError(f"{name}: selected pool generation is unavailable")
        generation = int(generation_text)
        info = self.generation_info(name, generation)
        if info.get("system") != domain["backend"]["system"]["source"]:
            raise DeploymentError(
                f"{name}: selected pool generation differs from release"
            )
        now = time.time()
        identity = f"{transaction}-{name}"
        record = {
            "schema": TRASH_SCHEMA,
            "id": identity,
            "status": "planned",
            "domain": name,
            "uuid": self.command("qvm-prefs", name, "uuid"),
            "domain_config": domain,
            "source_release": str(release),
            "source_transaction": transaction,
            "pool_generation": generation,
            "retired_at": None,
            "purge_after": now + retention_days * 86400,
            "retention_days": retention_days,
            "storage": {
                "disposition": "registered-stopped",
                "private": domain.get("backend", {}).get("state", {}).get("persistent"),
                "pool_manifest": info.get("manifest"),
            },
        }
        atomic_json(
            self.state / "transactions" / transaction / "trash-plan.json", record
        )
        if str(release).startswith("/nix/store/"):
            self.pin("trash-" + identity, release)
        return record

    def retire(self, record):
        path = self.trash_path(record["id"])
        if path.exists():
            current = read_json(path)
            if current.get("domain_config") != record.get("domain_config"):
                raise DeploymentError(
                    f"{record['domain']}: existing trash record differs"
                )
            record = current
        record["status"] = "retiring"
        atomic_json(path, record)
        name = record["domain"]
        self.command("qvm-prefs", name, "autostart", "False")
        self.command("qvm-tags", name, "add", "airlock-retired")
        self.command(
            "airlock-closure-store",
            "set-retention",
            "--domain",
            name,
            "--generation",
            str(record["pool_generation"]),
            "--retain",
            "true",
        )
        if record.get("retired_at") is None:
            record["retired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["status"] = "trashed"
        atomic_json(path, record)
        return record

    def validate_restore(self, name, domain, record):
        if record.get("status") == "purging":
            raise DeploymentError(f"{name}: trash purge is already in progress")
        previous = record["domain_config"]
        if any(previous.get(key) != domain.get(key) for key in ("class", "template")):
            raise DeploymentError(f"{name}: restore changes Qubes identity")
        if previous.get("backend", {}).get("state") != domain.get("backend", {}).get(
            "state"
        ):
            raise DeploymentError(f"{name}: restore changes private storage identity")
        if self.command("qvm-prefs", name, "uuid") != record.get("uuid"):
            raise DeploymentError(f"{name}: registered VM identity differs from trash")

    def restore(self, record, release, transaction):
        name = record["domain"]
        self.command("qvm-tags", name, "del", "airlock-retired")
        record["status"] = "restored"
        record["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["restored_by_release"] = str(release)
        record["restored_by_transaction"] = transaction
        record["storage"]["disposition"] = "active"
        atomic_json(self.trash_path(record["id"]), record)
        root = self.state / "roots" / ("trash-" + record["id"])
        root.unlink(missing_ok=True)
        return record

    def enforce_trash(self):
        records = self.active_trash()
        running = self.running()
        domains = set(self.command("qvm-ls", "--raw-list").splitlines())
        for name in sorted(records):
            if name not in domains:
                if records[name].get("status") == "purging":
                    continue
                raise DeploymentError(f"trashed VM is missing: {name}")
            if name in running:
                self.command(
                    "qvm-shutdown", "--wait", "--timeout", "120", name, timeout=150
                )
                raise DeploymentError(
                    f"trashed VM was running and has been stopped: {name}"
                )
            self.command("qvm-prefs", name, "autostart", "False")
            tags = self.command("qvm-tags", name, "list").splitlines()
            if "airlock-retired" not in tags:
                self.command("qvm-tags", name, "add", "airlock-retired")
        return [records[name] for name in sorted(records)]

    def purge_trash(self, identity, confirmation, now=None):
        if confirmation != identity:
            raise DeploymentError("trash purge confirmation does not match record id")
        with self.locked():
            path = self.trash_path(identity)
            if not path.exists():
                raise DeploymentError("trash record does not exist")
            record = read_json(path)
            if record.get("schema") != TRASH_SCHEMA or record.get("id") != identity:
                raise DeploymentError("invalid trash record")
            if record.get("status") not in ("trashed", "purging"):
                raise DeploymentError("trash record is not eligible for purge")
            now = time.time() if now is None else now
            if now < record["purge_after"]:
                raise DeploymentError("trash retention window has not expired")
            name = record["domain"]
            selected = self.pointer() or {}
            for key in ("release", "previous"):
                release = selected.get(key)
                if release and name in load_release(release)[1]["domains"]:
                    raise DeploymentError(
                        f"{name}: retained {key} deployment still references VM"
                    )
            # Every transaction release remains GC-rooted. An older release is
            # still a possible rollback source even after it leaves the
            # current/previous selection pair.
            roots = self.state / "roots"
            if roots.exists():
                for root in roots.iterdir():
                    if not root.is_symlink():
                        continue
                    release = root.resolve()
                    if (release / "release.json").exists() and name in load_release(
                        release
                    )[1]["domains"]:
                        raise DeploymentError(
                            f"{name}: retained deployment root {root.name} still references VM"
                        )
            if name in self.running():
                raise DeploymentError(f"{name}: cannot purge a running VM")
            record["status"] = "purging"
            atomic_json(path, record)
            domains = set(self.command("qvm-ls", "--raw-list").splitlines())
            if name in domains:
                if self.command("qvm-prefs", name, "uuid") != record.get("uuid"):
                    raise DeploymentError(
                        f"{name}: registered VM identity differs from trash"
                    )
                self.command("qvm-remove", "--force", name)
            self.command(
                "airlock-closure-store",
                "retire-generation",
                "--domain",
                name,
                "--generation",
                str(record["pool_generation"]),
            )
            root = self.state / "roots" / ("trash-" + identity)
            root.unlink(missing_ok=True)
            record["status"] = "purged"
            record["purged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            record["storage"]["disposition"] = "purged"
            atomic_json(path, record)
            return record

    def baseline(self):
        selected = self.pointer()
        if selected:
            return Path(selected["release"])
        baseline = self.state / "baseline"
        baseline.mkdir(exist_ok=True)
        source = Path("/var/lib/airlock/system/current-topology.json")
        if not source.exists():
            raise DeploymentError("bootstrap has not completed; no current topology")
        profile = active_profile()
        host = str(Path("/run/current-system").resolve())
        setup = str(Path("/run/current-system/sw/bin/airlock-setup-config").resolve())
        coordinator = str(
            Path("/run/current-system/sw/bin/airlock-machine-deploy").resolve()
        )
        atomic_json(baseline / "topology.json", read_json(source))
        atomic_json(
            baseline / "release.json",
            {
                "schema": SCHEMA,
                "profile": profile,
                "host": host,
                "setup": setup,
                "coordinator": coordinator,
                "revision": "booted-image-baseline",
                "lockHash": None,
                "retireDomains": [],
                "trashRetentionDays": DEFAULT_TRASH_RETENTION_DAYS,
            },
        )
        self.pin("baseline-host", host)
        return baseline

    def inspect(self, release):
        desired, new = load_release(release)
        current = self.pointer()
        if current:
            previous, old = load_release(current["release"])
            profile = previous["profile"]
        elif desired.get("baseRelease"):
            previous, old = load_release(desired["baseRelease"])
            profile = previous["profile"]
        else:
            previous, old = load_release(self.baseline())
            profile = previous["profile"]
        if profile != desired["profile"]:
            raise DeploymentError(
                f"profile mismatch: booted {profile}, release {desired['profile']}"
            )
        result = plan(
            old,
            new,
            self.running(),
            retire_removed=desired.get("retireDomains", []),
            trashed=self.active_trash(),
        )
        result["retirement"] = self.retirement_details(
            old,
            result["retire"],
            desired.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS),
        )
        result["host_reboot_required"] = (
            str(Path("/run/current-system").resolve()) != desired["host"]
        )
        result["revision"] = desired["revision"]
        return result

    def generation_info(self, domain, generation):
        result = json.loads(
            self.command(
                "airlock-closure-store",
                "describe-generation",
                "--domain",
                domain,
                "--generation",
                str(generation),
            )
        )
        if (
            result.get("schema") != GENERATION_INFO_SCHEMA
            or result.get("status") != "passed"
        ):
            raise DeploymentError("closure store returned invalid generation details")
        return result

    def validate_staged_generation(
        self, info, domain, generation, backend, release_manifest
    ):
        store = backend["store"]
        expected = {
            "domain": domain,
            "generation": generation,
            "system": backend["system"]["source"],
            "root_source": backend["root"]["source"],
            "kernel_name": backend["kernel"]["name"],
            "closure_source": store["source"],
        }
        if any(info.get(key) != value for key, value in expected.items()):
            raise DeploymentError(f"{domain}: staged generation identity mismatch")
        expected_store = {
            "mode": "shared-closure",
            "mountpoint": store["mountpoint"],
            "device": store["device"],
            "blob_device": store["blobDevice"],
            "view_backend": store["viewBackend"],
        }
        if any(
            info.get("store", {}).get(key) != value
            for key, value in expected_store.items()
        ):
            raise DeploymentError(f"{domain}: staged generation storage mismatch")
        approval = info.get("approval", {})
        provenance = info.get("provenance", {})
        if (
            approval.get("approved_by") != "sys-dev"
            or approval.get("transport") != "qrexec"
            or approval.get("source") != store["source"]
        ):
            raise DeploymentError(f"{domain}: staged generation approval mismatch")
        if provenance.get("source_revision") != release_manifest[
            "revision"
        ] or provenance.get("lock_hash") != release_manifest.get("lockHash"):
            raise DeploymentError(f"{domain}: staged generation provenance mismatch")
        if info.get("verification", {}).get("status") != "passed":
            raise DeploymentError(f"{domain}: staged generation is not verified")
        if info.get("retention", {}).get("retain") is not True:
            raise DeploymentError(f"{domain}: staged generation is not retained")

    def guest_candidate(self, receipt):
        required = {
            "schema": ADMISSION_SCHEMA,
            "status": "passed",
            "transport": "qrexec",
            "activated": False,
            "retained": True,
        }
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != value for key, value in required.items()
        ):
            raise DeploymentError("receipt is not a retained inactive qrexec admission")
        domain = receipt.get("domain")
        generation = receipt.get("generation")
        revision = receipt.get("source_revision")
        lock_hash = receipt.get("lock_hash")
        if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", domain):
            raise DeploymentError("invalid receipt domain")
        if not isinstance(generation, int) or generation < 1:
            raise DeploymentError("invalid receipt generation")
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", revision
        ):
            raise DeploymentError("invalid receipt source revision")
        if not isinstance(lock_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", lock_hash
        ):
            raise DeploymentError("invalid receipt lock hash")

        baseline = self.baseline()
        current_release, topology = load_release(baseline)
        if domain not in topology["domains"]:
            raise DeploymentError(f"receipt target is not declared: {domain}")
        backend = topology["domains"][domain].get("backend", {})
        if (
            backend.get("type") != "nix-closure"
            or backend.get("store", {}).get("mode") != "shared-closure"
        ):
            raise DeploymentError(f"{domain}: receipt requires shared-closure storage")

        info = self.generation_info(domain, generation)
        receipt_identity = {
            "domain": domain,
            "generation": generation,
            "manifest": receipt.get("manifest"),
            "system": receipt.get("system"),
            "root_source": receipt.get("root_source"),
            "kernel_name": receipt.get("kernel_name"),
            "closure_hash": receipt.get("closure_hash"),
        }
        info_identity = {
            "domain": info.get("domain"),
            "generation": info.get("generation"),
            "manifest": info.get("manifest"),
            "system": info.get("system"),
            "root_source": info.get("root_source"),
            "kernel_name": info.get("kernel_name"),
            "closure_hash": info.get("store", {}).get("closure_hash"),
        }
        if info_identity != receipt_identity:
            raise DeploymentError("receipt does not identify the admitted generation")
        if info.get("selected") is not False or generation == receipt.get(
            "current_generation"
        ):
            raise DeploymentError("receipt generation is already active")
        provenance = info.get("provenance", {})
        if (
            provenance.get("source_revision") != revision
            or provenance.get("lock_hash") != lock_hash
        ):
            raise DeploymentError("receipt provenance differs from pool generation")
        if (
            not isinstance(info.get("closure_source"), str)
            or not info["closure_source"]
        ):
            raise DeploymentError("pool generation has no closure source identity")

        candidate_topology = deepcopy(topology)
        candidate_backend = candidate_topology["domains"][domain]["backend"]
        candidate_backend["system"]["source"] = info["system"]
        candidate_backend["store"]["source"] = info["closure_source"]
        prefs = candidate_topology["domains"][domain].get("prefs", {})
        opts = prefs.get("kernelopts", "")
        matches = re.findall(r"(?<!\S)airlock\.system=(\S+)", opts)
        if matches:
            if matches != [backend["system"]["source"]]:
                raise DeploymentError(
                    "baseline guest boot system differs from topology"
                )
            prefs["kernelopts"] = re.sub(
                r"(?<!\S)airlock\.system=\S+",
                lambda _: "airlock.system=" + info["system"],
                opts,
            )
        candidate_manifest = deepcopy(current_release)
        if os.environ.get("AIRLOCK_MACHINE_COORDINATOR"):
            candidate_manifest["coordinator"] = os.environ[
                "AIRLOCK_MACHINE_COORDINATOR"
            ]
        candidate_manifest.update(
            revision=revision,
            lockHash=lock_hash,
            baseRelease=str(baseline),
            stagedGenerations={domain: generation},
            guestAdmission={
                "domain": domain,
                "generation": generation,
                "manifest": info["manifest"],
            },
        )
        self.validate_staged_generation(
            info, domain, generation, candidate_backend, candidate_manifest
        )
        if backend["system"]["source"] == info["system"]:
            raise DeploymentError("admitted guest system is already selected")

        identity = hashlib.sha256(
            json.dumps(
                {
                    "baseline": str(baseline),
                    "receipt": receipt_identity,
                    "revision": revision,
                    "lock_hash": lock_hash,
                    "topology": candidate_topology,
                    "release": candidate_manifest,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        candidate = self.state / "candidates" / identity
        expected = {
            "release.json": candidate_manifest,
            "topology.json": candidate_topology,
            "staged.json": {domain: generation},
            "receipt.json": receipt,
        }
        if candidate.exists():
            if any(
                not (candidate / name).is_file() or read_json(candidate / name) != value
                for name, value in expected.items()
            ):
                raise DeploymentError("existing guest candidate differs from receipt")
        else:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            temporary = candidate.parent / f".{identity}.new-{time.time_ns()}"
            temporary.mkdir()
            try:
                for name, value in expected.items():
                    atomic_json(temporary / name, value)
                temporary.rename(candidate)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        return candidate

    def inspect_guest(self, receipt):
        candidate = self.guest_candidate(receipt)
        return {"candidate": str(candidate), "plan": self.inspect(candidate)}

    def install_boot_hook(self, release, baseline):
        """Repository-owned compatibility bridge for already-flashed images."""
        manifest, _ = load_release(release)
        fallback, _ = load_release(baseline)
        controller = manifest.get("coordinator")
        if controller:
            controller_path = Path(controller).resolve()
            if str(controller_path).startswith("/nix/store/"):
                package = Path("/nix/store") / controller_path.parts[3]
                self.pin(package.name, package)
            link = self.state / "controller.new"
            link.unlink(missing_ok=True)
            link.symlink_to(controller)
            link.replace(self.state / "controller")
        changed = False
        for unit, scope in (
            ("airlock-setup-config", ""),
            ("airlock-setup-config-deferred", " --deferred"),
        ):
            # Do not enable units absent from this profile.
            if (
                self.command(
                    "systemctl", "show", unit, "--property=LoadState", "--value"
                )
                == "not-found"
            ):
                continue
            start = self.command(
                "systemctl", "show", unit, "--property=ExecStart", "--value"
            )
            if "/airlock-machine-deploy boot-setup " in start:
                # Current NixOS images already declare this hook. Their
                # /etc/systemd/system points into the immutable Nix store;
                # never try to place a compatibility drop-in inside it.
                continue
            dropin = Path(
                "/etc/systemd/system", unit + ".service.d", "90-airlock-machine.conf"
            )
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text(
                "# Generated by repository-owned airlock-machine-deploy.\n"
                "[Service]\nExecStart=\nExecStart="
                + str(self.state / "controller")
                + " boot-setup "
                + fallback["setup"]
                + " --apply"
                + scope
                + "\n"
            )
            changed = True
        if changed:
            self.command("systemctl", "daemon-reload")

    def stage(self, release, topology, names, directory):
        release_manifest, _ = load_release(release)
        preferred = release_manifest.get("stagedGenerations", {})
        unexpected = set(preferred) - set(names)
        if unexpected:
            raise DeploymentError(
                "staged generation is not part of this plan: "
                + ", ".join(sorted(unexpected))
            )
        staged = {}
        for name in names:
            backend = topology["domains"][name]["backend"]
            store = backend["store"]
            artifact = store["source"]
            if name in preferred:
                generation = preferred[name]
                info = self.generation_info(name, generation)
                self.validate_staged_generation(
                    info, name, generation, backend, release_manifest
                )
                staged[name] = generation
                atomic_json(directory / "staged.json", staged)
                continue
            # Image rollback uses the retained, already admitted seed descriptor.
            if (Path(artifact) / "prepared.json").exists():
                continue
            match = json.loads(
                self.command(
                    "airlock-closure-store",
                    "find-generation",
                    "--domain",
                    name,
                    "--system",
                    backend["system"]["source"],
                    "--closure-source",
                    artifact,
                    "--root-source",
                    backend["root"]["source"],
                    "--kernel-name",
                    backend["kernel"]["name"],
                    "--view-backend",
                    store["viewBackend"],
                    "--approved-by",
                    "sys-dev",
                )
            )
            if match["status"] == "found":
                staged[name] = match["generation"]
                atomic_json(directory / "staged.json", staged)
                continue
            output = self.command(
                "airlock-closure-store",
                "admit",
                "--verify-mode",
                "incremental",
                "--domain",
                name,
                "--system",
                backend["system"]["source"],
                "--closure-source",
                artifact,
                "--root-source",
                backend["root"]["source"],
                "--kernel-name",
                backend["kernel"]["name"],
                "--store-device",
                store["device"],
                "--store-blob-device",
                store["blobDevice"],
                "--store-mountpoint",
                store["mountpoint"],
                "--view-backend",
                store["viewBackend"],
                "--approval-source",
                artifact,
                "--activate",
                "false",
                "--retain",
                "true",
            )
            staged[name] = json.loads(output)["generation"]
            atomic_json(directory / "staged.json", staged)
        atomic_json(directory / "staged.json", staged)

    def setup(self, release, directory):
        manifest, _ = load_release(release)
        env = dict(
            os.environ,
            AIRLOCK_LOG_DIR=str(directory / "setup"),
            AIRLOCK_STAGED_GENERATIONS=str(directory / "staged.json"),
        )
        self.command(
            manifest["setup"],
            "--apply",
            "--topology",
            str(release / "topology.json"),
            env=env,
        )
        result = read_json(directory / "setup" / "setup-config.json")
        if result.get("unsupported_domains_count") or result.get("restart_required"):
            raise DeploymentError(
                "setup left unsupported or deferred work; inspect setup-config.json"
            )

    def verify(self, topology, expected_running):
        observed = {}
        running = self.running()
        for name, domain in topology["domains"].items():
            expected = domain["backend"]["system"]["source"]
            selected = self.command("qvm-features", name, "airlock-system-source")
            if selected != expected:
                raise DeploymentError(
                    f"{name}: selected system {selected!r}, expected {expected}"
                )
            actual = None
            if name in expected_running:
                if name not in running:
                    raise DeploymentError(f"{name}: failed to start")
                actual = self.command(
                    "qrexec-client",
                    "-d",
                    name,
                    "user:readlink -f /run/current-system",
                    timeout=180,
                )
                if actual != expected:
                    raise DeploymentError(
                        f"{name}: running system differs from release: {actual}"
                    )
            observed[name] = {"selected": selected, "running": actual}
        return observed

    def apply(self, release, recover=False):
        release = Path(release).resolve()
        manifest, topology = load_release(release)
        with self.locked():
            latest = self.state / "latest.json"
            if latest.exists() and not recover:
                existing = read_json(latest)
                if existing["phase"] in ("complete", "reboot-required") and existing[
                    "release"
                ] == str(release):
                    self.verify(topology, set(existing["plan"]["start"]))
                    self.enforce_trash()
                    return existing
            if (
                latest.exists()
                and read_json(latest)["phase"] not in ("complete", "reboot-required")
                and not recover
            ):
                raise DeploymentError(
                    "unfinished deployment: inspect status, then use resume or rollback"
                )
            baseline = self.baseline()
            interrupted = read_json(latest) if recover and latest.exists() else None
            # Resume compares against the pre-transaction topology, not the
            # selected-but-possibly-only-half-installed topology.
            if interrupted and str(release) == interrupted["release"]:
                baseline = Path(interrupted["previous"])
            previous, old = load_release(baseline)
            profile = active_profile()
            if manifest["profile"] != profile or previous["profile"] != profile:
                raise DeploymentError("deployment profile differs from booted profile")
            runtime = self.running()
            wanted_running = runtime | (
                set(interrupted["plan"]["start"]) if interrupted else set()
            )
            rollback = (
                interrupted is not None and str(release) != interrupted["release"]
            )
            changes = plan(
                old,
                topology,
                wanted_running,
                retire_removed=manifest.get("retireDomains", []),
                trashed=self.active_trash(),
                retain_removed=rollback,
            )
            changes["stop"] = [name for name in changes["stop"] if name in runtime]
            changes["retirement"] = self.retirement_details(
                old,
                changes["retire"],
                manifest.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS),
            )
            active_trash = self.active_trash()
            restore_plan = []
            for name in changes["restore"]:
                trash = active_trash[name]
                self.validate_restore(name, topology["domains"][name], trash)
                restore_plan.append(trash)
            transaction = str(time.time_ns())
            directory = self.state / "transactions" / transaction
            directory.mkdir(parents=True)
            record = {
                "id": transaction,
                "release": str(release),
                "previous": str(baseline),
                "revision": manifest["revision"],
                "plan": changes,
                "phase": "preflight",
            }

            def phase(value, **extra):
                record.update(phase=value, **extra)
                atomic_json(directory / "status.json", record)
                atomic_json(latest, record)
                print(
                    json.dumps({"transaction": transaction, "phase": value}), flush=True
                )

            phase("staging")
            try:
                if str(release).startswith("/nix/store/"):
                    self.pin(transaction, release)
                self.install_boot_hook(release, baseline)
                self.stage(release, topology, changes["stage"], directory)
                trash_plan = [
                    self.prepare_retirement(
                        name,
                        old["domains"][name],
                        baseline,
                        transaction,
                        manifest.get(
                            "trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS
                        ),
                    )
                    for name in changes["retire"]
                ]
                # Selection is written before disruption. Boot fails closed on an
                # interrupted transaction instead of restoring the image topology.
                atomic_json(
                    self.state / "selection.json",
                    {
                        "release": str(release),
                        "previous": str(baseline),
                        "transaction": transaction,
                        "profile": manifest["profile"],
                    },
                )
                phase("stopping")
                for name in changes["stop"]:
                    self.command(
                        "qvm-shutdown", "--wait", "--timeout", "120", name, timeout=150
                    )
                phase("retiring")
                retired = [self.retire(entry) for entry in trash_plan]
                phase("configuring")
                self.setup(release, directory)
                restored = [
                    self.restore(entry, release, transaction) for entry in restore_plan
                ]
                phase("host-boot-selection")
                self.command(
                    "nix-env",
                    "-p",
                    "/nix/var/nix/profiles/system",
                    "--set",
                    manifest["host"],
                )
                self.command(
                    manifest["host"] + "/bin/switch-to-configuration",
                    "boot",
                    timeout=600,
                )
                phase("starting")
                for name in changes["start"]:
                    if name not in self.running():
                        self.command("qvm-start", name, timeout=240)
                phase("verifying")
                observed = self.verify(topology, set(changes["start"]))
                pending = str(Path("/run/current-system").resolve()) != manifest["host"]
                phase(
                    "reboot-required" if pending else "complete",
                    observed=observed,
                    trash={
                        "retired": [entry["id"] for entry in retired],
                        "restored": [entry["id"] for entry in restored],
                    },
                    host_running=str(Path("/run/current-system").resolve()),
                    host_selected=manifest["host"],
                )
            except Exception as exc:
                phase("failed", error=str(exc), failed_phase=record["phase"])
                raise
        return record

    def status(self):
        path = self.state / "latest.json"
        result = read_json(path) if path.exists() else {"phase": "image-baseline"}
        result["selection"] = self.pointer()
        result["host_running_now"] = str(Path("/run/current-system").resolve())
        result["host_matches_selection"] = (
            result.get("host_selected") == result["host_running_now"]
        )
        return result

    def verify_selected(self):
        with self.locked():
            selected = self.pointer()
            if not selected:
                raise DeploymentError("no selected deployment")
            directory = self.state / "transactions" / selected["transaction"]
            record = read_json(directory / "status.json")
            if record["phase"] not in ("complete", "reboot-required"):
                raise DeploymentError("unfinished deployment; resume or rollback first")
            manifest, topology = load_release(selected["release"])
            record["observed"] = self.verify(topology, set(record["plan"]["start"]))
            record["trash_active"] = [entry["id"] for entry in self.enforce_trash()]
            record["host_running"] = str(Path("/run/current-system").resolve())
            record["phase"] = (
                "complete"
                if record["host_running"] == manifest["host"]
                else "reboot-required"
            )
            record["verified_at"] = time.time()
            atomic_json(directory / "status.json", record)
            atomic_json(self.state / "latest.json", record)
            return record

    def boot_start(self, default_topology, require_running=()):
        """Start declared autostart guests only after boot setup/GPU guards."""
        with self.locked():
            selected = self.pointer()
            if selected:
                if selected["profile"] != active_profile():
                    raise DeploymentError(
                        "selected deployment belongs to another boot profile"
                    )
                status = read_json(
                    self.state
                    / "transactions"
                    / selected["transaction"]
                    / "status.json"
                )
                if status["phase"] not in ("complete", "reboot-required"):
                    raise DeploymentError(
                        "interrupted deployment; resume or rollback first"
                    )
                _, topology = load_release(selected["release"])
            else:
                topology = read_json(default_topology)
            trash = self.enforce_trash()
            active_trash = {entry["domain"]: entry for entry in trash}
            declared_trash = set(topology["domains"]) & set(active_trash)
            if declared_trash:
                raise DeploymentError(
                    "selected topology still declares trashed VM(s): "
                    + ", ".join(sorted(declared_trash))
                )
            running = self.running()
            missing = set(require_running) - running
            if missing:
                raise DeploymentError(
                    "guarded VMs are not running: " + ", ".join(sorted(missing))
                )
            # No previous-session running set: cold boot follows autostart only.
            starts = plan(topology, topology, set(), trashed=active_trash)["start"]
            started = []
            for name in starts:
                if name not in running:
                    self.command("qvm-start", name, timeout=180)
                    started.append(name)
            observed = self.verify(topology, set(starts) | set(require_running))
            return {
                "autostart": starts,
                "started": started,
                "observed": observed,
                "trash_active": [entry["id"] for entry in trash],
            }

    def boot_setup(self, default_setup, args):
        with self.locked() as lock:
            # Keep the lock across exec through the exact setup wrapper.
            os.set_inheritable(lock.fileno(), True)
            self._boot_setup(default_setup, args)

    def _boot_setup(self, default_setup, args):
        selected = self.pointer()
        if not selected:
            return os.execv(default_setup, [default_setup, *args])
        release, _ = load_release(selected["release"])
        profile = active_profile()
        if selected["profile"] != profile:
            # Recovery/other profiles must not adopt a lab deployment implicitly.
            raise DeploymentError(
                "selected deployment belongs to another boot profile; use recovery"
            )
        status = read_json(
            self.state / "transactions" / selected["transaction"] / "status.json"
        )
        if status["phase"] not in ("complete", "reboot-required"):
            raise DeploymentError(
                "interrupted deployment; use machine-deploy resume/rollback in recovery"
            )
        setup = release["setup"]
        staged = Path(selected["release"]) / "staged.json"
        if release.get("stagedGenerations"):
            if not staged.exists() or read_json(staged) != release["stagedGenerations"]:
                raise DeploymentError(
                    "selected release staged generation map is missing"
                )
            os.environ["AIRLOCK_STAGED_GENERATIONS"] = str(staged)
        else:
            os.environ.pop("AIRLOCK_STAGED_GENERATIONS", None)
        os.execv(
            setup,
            [
                setup,
                *args,
                "--topology",
                str(Path(selected["release"]) / "topology.json"),
            ],
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        sub.add_parser(action).add_argument("release", type=Path)
    for action in ("guest-plan", "guest-apply"):
        sub.add_parser(action).add_argument("receipt")
    for action in ("status", "verify", "resume", "rollback", "trash-list"):
        sub.add_parser(action)
    purge = sub.add_parser("trash-purge")
    purge.add_argument("identity")
    purge.add_argument("--confirm", required=True)
    boot = sub.add_parser("boot-setup")
    boot.add_argument("setup")
    boot.add_argument("args", nargs=argparse.REMAINDER)
    start = sub.add_parser("boot-start")
    start.add_argument("--topology", default="/etc/airlock/qubes-topology.json")
    start.add_argument("--require-running", action="append", default=[])
    args = parser.parse_args(argv)
    coordinator = Coordinator()
    try:
        if args.action == "plan":
            result = coordinator.inspect(args.release)
        elif args.action == "apply":
            result = coordinator.apply(args.release)
        elif args.action in ("guest-plan", "guest-apply"):
            receipt = (
                json.load(sys.stdin) if args.receipt == "-" else read_json(args.receipt)
            )
            candidate = coordinator.guest_candidate(receipt)
            result = (
                coordinator.apply(candidate)
                if args.action == "guest-apply"
                else {
                    "candidate": str(candidate),
                    "plan": coordinator.inspect(candidate),
                }
            )
        elif args.action in ("resume", "rollback"):
            status = coordinator.status()
            key = "release" if args.action == "resume" else "previous"
            if key not in status:
                raise DeploymentError("no deployment to resume or roll back")
            result = coordinator.apply(status[key], recover=True)
        elif args.action == "boot-setup":
            coordinator.boot_setup(args.setup, args.args)
            return 0
        elif args.action == "boot-start":
            result = coordinator.boot_start(args.topology, args.require_running)
        elif args.action == "verify":
            result = coordinator.verify_selected()
        elif args.action == "trash-list":
            result = {"schema": TRASH_SCHEMA, "records": coordinator.trash_records()}
        elif args.action == "trash-purge":
            result = coordinator.purge_trash(args.identity, args.confirm)
        else:
            result = coordinator.status()
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DeploymentError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"airlock-machine-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
