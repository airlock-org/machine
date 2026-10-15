"""Candidate-bound guest admission through the installed host interface."""

import hashlib
import json
from pathlib import Path
import re

from .common import DeploymentError
from .planning import domain_references
from .state import atomic_json, load_release, read_json

SCHEMA = "airlock.machine-admission.v1"
STORE_PATH = re.compile(r"/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+")
DOMAIN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,30}")


class AdmissionManager:
    def __init__(self, coordinator):
        self.machine = coordinator

    def state(self):
        latest = self.machine.state / "latest.json"
        record = read_json(latest) if latest.exists() else None
        if record and record["phase"] not in ("complete", "reboot-required"):
            raise DeploymentError("finish or recover the current deployment first")
        return {"selection": self.machine.pointer(), "latest": record}

    def prepare(self, release, caller, boot_id):
        if caller != "sys-ctrl":
            raise DeploymentError("candidate admission requires sys-ctrl")
        with self.machine.locked("Artifact admission", "Validate a release before importing artifacts",
                                 "admission-prepare", operation_id=str(release)):
            self.machine.releases.require_no_pruning()
            self.machine.releases.require_available_release(release)
            state = self.state()
            manifest, topology = load_release(release)
            if manifest.get("guestTransport") != "direct-pool-v1":
                raise DeploymentError("candidate requires direct pool admission")
            if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", "")):
                raise DeploymentError("invalid candidate revision")
            if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("lockHash", "")):
                raise DeploymentError("invalid candidate lock hash")
            domains = topology["domains"]
            if not isinstance(domains, dict) or not 1 <= len(domains) <= 4096:
                raise DeploymentError("invalid candidate domain inventory")
            roots = read_json(Path(release) / "root-artifacts.json")
            if not isinstance(roots, dict) or set(roots) != set(domains):
                raise DeploymentError("candidate root inventory differs from domains")
            for name in domains:
                if not isinstance(name, str) or not DOMAIN.fullmatch(name) or name == "dom0":
                    raise DeploymentError("invalid candidate domain name")
            for name, refs in domain_references(topology).items():
                if refs - set(domains) - {"dom0"}:
                    raise DeploymentError(f"{name}: candidate has undeclared dependencies")
            # Existing transition validation includes backend/storage restrictions,
            # retirement references, dependency cycles and profile compatibility.
            plan = self.machine.inspect(release)
            guests = {}
            for name, domain in domains.items():
                from airlock_qubes_storage.root_format import validate_descriptor
                try:
                    validate_descriptor(roots[name])
                except ValueError as exc:
                    raise DeploymentError(f"{name}: invalid candidate root descriptor") from exc
                backend = domain["backend"]
                identity = {
                    "domain": name,
                    "system": backend["system"]["source"],
                    "closure_source": backend["store"]["source"],
                    "root_source": backend["root"]["source"],
                    "kernel_name": backend["kernel"]["name"],
                    "source_revision": manifest["revision"],
                    "lock_hash": manifest["lockHash"],
                    "root_artifact": roots[name],
                }
                if (not STORE_PATH.fullmatch(identity["system"])
                        or not STORE_PATH.fullmatch(identity["closure_source"])
                        or not identity["root_source"].endswith("/root.img")
                        or not STORE_PATH.fullmatch(identity["root_source"][:-9])
                        or not isinstance(roots[name], dict)):
                    raise DeploymentError(f"{name}: invalid candidate artifact identity")
                guests[name] = identity
            record = {"schema": SCHEMA, "release": str(Path(release).resolve()),
                      "caller": caller, "boot_id": boot_id, "state": state,
                      "guests": guests}
            token = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            # Retain the imported transport before making its authorization usable.
            self.machine.pin("admission-" + token, release)
            atomic_json(self.machine.state / "admissions" / (token + ".json"), record)
            return {"schema": SCHEMA, "admission": token, "domains": sorted(guests),
                    "release": record["release"], "plan": plan, "activated": False}

    def identity(self, token, domain, caller, boot_id):
        if not re.fullmatch(r"[0-9a-f]{64}", token) or not DOMAIN.fullmatch(domain):
            raise DeploymentError("invalid candidate admission argument")
        record = read_json(self.machine.state / "admissions" / (token + ".json"))
        if hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest() != token:
            raise DeploymentError("candidate admission checksum mismatch")
        if record.get("schema") != SCHEMA or record.get("caller") != caller or caller != "sys-ctrl":
            raise DeploymentError("candidate admission caller mismatch")
        if record["boot_id"] != boot_id or record["state"] != self.state():
            raise DeploymentError("candidate admission is stale; prepare the release again")
        self.machine.releases.require_no_pruning()
        self.machine.releases.require_available_release(record["release"])
        if domain not in record["guests"]:
            raise DeploymentError("domain is not in the prepared candidate")
        return record["guests"][domain]
