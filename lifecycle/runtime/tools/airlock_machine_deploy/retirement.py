"""VM retirement, restoration, and resumable trash purging.

Public destructive operations acquire the coordinator lock. Helpers invoked by
apply/boot operate under the caller's existing lock; they never acquire it again.
"""

from pathlib import Path
import re
import time
from typing import TYPE_CHECKING

from .common import DeploymentError, TRASH_SCHEMA
from .planning import references_domain
from .validation import validate_volume_ownership
from .state import read_json, atomic_json, load_release
from .models import RetirementDetail, TrashRecord

if TYPE_CHECKING:
    from .coordinator import Coordinator


class RetirementManager:
    def __init__(self, machine: "Coordinator") -> None:
        self.machine = machine

    def trash_records(self) -> list[TrashRecord]:
        records = []
        directory = self.machine.state / "trash"
        if not directory.exists():
            return records
        for path in sorted(directory.glob("*.json")):
            record = read_json(path)
            if record.get("schema") != TRASH_SCHEMA or record.get("id") != path.stem:
                raise DeploymentError(f"invalid machine trash record: {path.name}")
            records.append(record)
        return records

    def active_trash(self) -> dict[str, TrashRecord]:
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
        return self.machine.state / "trash" / f"{identity}.json"

    def retirement_details(self, topology, names, retention_days) -> list[RetirementDetail]:
        if not names:
            return []
        if not Path(self.machine.retired_policy).is_file():
            raise DeploymentError("retired-domain qrexec policy is not installed")
        details = []
        for name in names:
            domain = topology["domains"][name]
            generation_text = self.machine.command(
                "qvm-features", name, "airlock-store-generation"
            )
            if not generation_text.isdigit():
                raise DeploymentError(
                    f"{name}: selected pool generation is unavailable"
                )
            generation = int(generation_text)
            info = self.machine.generation_info(name, generation)
            if info.get("system") != domain["backend"]["system"]["source"]:
                raise DeploymentError(
                    f"{name}: selected pool generation differs from release"
                )
            details.append(
                {
                    "domain": name,
                    "uuid": self.machine.command("qvm-prefs", name, "uuid"),
                    "private": domain.get("backend", {})
                    .get("state", {})
                    .get("persistent"),
                    "pool_generation": generation,
                    "pool_manifest": info.get("manifest"),
                    "retention_days": retention_days,
                }
            )
        return details

    def prepare_retirement(
        self, name, domain, release, transaction, retention_days
    ) -> TrashRecord:
        existing = self.active_trash().get(name)
        if existing:
            if existing.get("status") == "purging":
                raise DeploymentError(f"{name}: trash purge is already in progress")
            if existing.get("domain_config") != domain:
                raise DeploymentError(f"{name}: active trash identity differs")
            if self.machine.command("qvm-prefs", name, "uuid") != existing.get("uuid"):
                raise DeploymentError(
                    f"{name}: registered VM identity differs from trash"
                )
            return existing
        generation_text = self.machine.command("qvm-features", name, "airlock-store-generation")
        if not generation_text.isdigit():
            raise DeploymentError(f"{name}: selected pool generation is unavailable")
        generation = int(generation_text)
        info = self.machine.generation_info(name, generation)
        if info.get("system") != domain["backend"]["system"]["source"]:
            raise DeploymentError(
                f"{name}: selected pool generation differs from release"
            )
        now = time.time()
        identity = f"{transaction}-{name}"
        record: TrashRecord = {
            "schema": TRASH_SCHEMA,
            "id": identity,
            "status": "planned",
            "domain": name,
            "uuid": self.machine.command("qvm-prefs", name, "uuid"),
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
                "volumes": self.machine.volume_inventory()[name],
            },
        }
        atomic_json(
            self.machine.state / "transactions" / transaction / "trash-plan.json", record
        )
        if str(release).startswith("/nix/store/"):
            self.machine.pin("trash-" + identity, release)
        return record

    def retire(self, record: TrashRecord) -> TrashRecord:
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
        self.machine.command("qvm-prefs", name, "autostart", "False")
        self.machine.command("qvm-tags", name, "add", "airlock-retired")
        self.machine.command(
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
            now = time.time()
            record["retired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            record["purge_after"] = now + record["retention_days"] * 86400
        record["status"] = "trashed"
        atomic_json(path, record)
        return record

    def validate_restore(self, name, domain, record: TrashRecord) -> None:
        if record.get("status") == "purging":
            raise DeploymentError(f"{name}: trash purge is already in progress")
        previous = record["domain_config"]
        if any(previous.get(key) != domain.get(key) for key in ("class", "template")):
            raise DeploymentError(f"{name}: restore changes Qubes identity")
        if previous.get("backend", {}).get("state") != domain.get("backend", {}).get(
            "state"
        ):
            raise DeploymentError(f"{name}: restore changes private storage identity")
        if self.machine.command("qvm-prefs", name, "uuid") != record.get("uuid"):
            raise DeploymentError(f"{name}: registered VM identity differs from trash")

    def restore(self, record: TrashRecord, release, transaction) -> TrashRecord:
        name = record["domain"]
        self.machine.command("qvm-tags", name, "del", "airlock-retired")
        record["status"] = "restored"
        record["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["restored_by_release"] = str(release)
        record["restored_by_transaction"] = transaction
        record["storage"]["disposition"] = "active"
        atomic_json(self.trash_path(record["id"]), record)
        root = self.machine.state / "roots" / ("trash-" + record["id"])
        root.unlink(missing_ok=True)
        return record

    def enforce_trash(self) -> list[TrashRecord]:
        records = self.active_trash()
        running = self.machine.running()
        domains = set(self.machine.command("qvm-ls", "--raw-list").splitlines())
        for name in sorted(records):
            if name not in domains:
                if records[name].get("status") == "purging":
                    continue
                raise DeploymentError(f"trashed VM is missing: {name}")
            if name in running:
                self.machine.command(
                    "qvm-shutdown", "--wait", "--timeout", "120", name, timeout=150
                )
                raise DeploymentError(
                    f"trashed VM was running and has been stopped: {name}"
                )
            self.machine.command("qvm-prefs", name, "autostart", "False")
            tags = self.machine.command("qvm-tags", name, "list").splitlines()
            if "airlock-retired" not in tags:
                self.machine.command("qvm-tags", name, "add", "airlock-retired")
        return [records[name] for name in sorted(records)]

    def purge_trash(self, identity, confirmation, now=None) -> TrashRecord:
        if confirmation != identity:
            raise DeploymentError("trash purge confirmation does not match record id")
        with self.machine.locked():
            self.machine.releases.require_no_pruning()
            latest = self.machine.state / "latest.json"
            if latest.exists() and read_json(latest)["phase"] not in (
                "complete",
                "reboot-required",
            ):
                raise DeploymentError("unfinished deployment; resume or rollback first")
            path = self.trash_path(identity)
            if not path.exists():
                raise DeploymentError("trash record does not exist")
            record = read_json(path)
            if record.get("schema") != TRASH_SCHEMA or record.get("id") != identity:
                raise DeploymentError("invalid trash record")
            if record.get("status") == "purged":
                return record
            if record.get("status") not in ("trashed", "purging"):
                raise DeploymentError("trash record is not eligible for purge")
            now = time.time() if now is None else now
            if now < record["purge_after"]:
                raise DeploymentError("trash retention window has not expired")
            name = record["domain"]
            selected = self.machine.pointer() or {}
            for key in ("release", "previous"):
                release = selected.get(key)
                if release and name in load_release(release)[1]["domains"]:
                    raise DeploymentError(
                        f"{name}: retained {key} deployment still references VM"
                    )
            for release in self.machine.releases.release_inventory():
                if self.machine.releases.prune_record_path(release).exists():
                    continue
                topology = load_release(release)[1]
                if references_domain(topology, name):
                    raise DeploymentError(
                        f"{name}: retained deployment {release} still references VM"
                    )
            # Bootable system profiles are also recovery choices, including
            # profiles not created by this coordinator.
            hosts = (
                set(self.machine.releases.host_profiles().values())
                | self.machine.running_hosts()
            )
            for host in hosts:
                topology_path = Path(host) / "etc/airlock/qubes-topology.json"
                if not topology_path.is_file():
                    raise DeploymentError(
                        "cannot inspect retained host topology: " + host
                    )
                topology = read_json(topology_path)
                if references_domain(topology, name):
                    raise DeploymentError(
                        f"{name}: retained host profile still references VM: {host}"
                    )
            if name in self.machine.running():
                raise DeploymentError(f"{name}: cannot purge a running VM")
            domains = set(self.machine.command("qvm-ls", "--raw-list").splitlines())
            if name in domains:
                if self.machine.command("qvm-prefs", name, "uuid") != record.get("uuid"):
                    raise DeploymentError(
                        f"{name}: registered VM identity differs from trash"
                    )
                inventory = self.machine.volume_inventory()
                expected = record["storage"].get("volumes")
                validate_volume_ownership(name, expected, inventory)
                if (
                    "airlock-retired"
                    not in self.machine.command("qvm-tags", name, "list").splitlines()
                ):
                    raise DeploymentError(f"{name}: retirement tag is missing")
                if self.machine.command(
                    "qvm-features", name, "airlock-store-generation"
                ) != str(record["pool_generation"]):
                    raise DeploymentError(f"{name}: pool generation differs from trash")
                info = self.machine.generation_info(name, record["pool_generation"])
                if (
                    info.get("manifest") != record["storage"]["pool_manifest"]
                    or info.get("system")
                    != record["domain_config"]["backend"]["system"]["source"]
                ):
                    raise DeploymentError(f"{name}: pool identity differs from trash")
                record["status"] = "purging"
                atomic_json(path, record)
                self.machine.command("qvm-remove", "--force", name)
            elif record["status"] != "purging":
                raise DeploymentError(f"{name}: VM disappeared before purge began")
            self.machine.command(
                "airlock-closure-store",
                "retire-generation",
                "--domain",
                name,
                "--generation",
                str(record["pool_generation"]),
            )
            root = self.machine.state / "roots" / ("trash-" + identity)
            root.unlink(missing_ok=True)
            record["status"] = "purged"
            record["purged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            record["storage"]["disposition"] = "purged"
            atomic_json(path, record)
            return record
