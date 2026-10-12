"""Retained release inventory and resumable pruning.

Public destructive operations acquire the coordinator lock. Helpers invoked by
apply/boot operate under the caller's existing lock; they never acquire it again.
"""

import hashlib
import os
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING

from .common import DeploymentError
from .state import read_json, atomic_json, load_release
from .models import PrunePlan, PruneRecord

if TYPE_CHECKING:
    from .coordinator import Coordinator


class ReleaseManager:
    def __init__(self, machine: "Coordinator") -> None:
        self.machine = machine

    def release_key(self, release):
        return hashlib.sha256(str(Path(release).resolve()).encode()).hexdigest()

    def prune_record_path(self, release):
        return self.machine.state / "pruned-releases" / (self.release_key(release) + ".json")

    def require_available_release(self, release):
        if self.prune_record_path(release).exists():
            raise DeploymentError(
                "release was pruned or is being pruned; deploy a new committed release"
            )

    def read_prune_record(self, path) -> PruneRecord:
        record = read_json(path)
        if (
            record.get("schema") != "airlock.machine-release-prune.v1"
            or not isinstance(record.get("release"), str)
            or self.prune_record_path(record["release"]) != path
            or record.get("status") not in ("pruning", "pruned")
            or not isinstance(record.get("roots"), dict)
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name)
                or not isinstance(target, str)
                or not Path(target).is_absolute()
                for name, target in record["roots"].items()
            )
            or not isinstance(record.get("profile_generations"), list)
            or any(
                type(number) is not int or number < 1
                for number in record["profile_generations"]
            )
            or record.get("host") != record.get("manifest", {}).get("host")
        ):
            raise DeploymentError("invalid release pruning journal")
        return record

    def require_no_pruning(self):
        for path in (self.machine.state / "pruned-releases").glob("*.json"):
            if self.read_prune_record(path)["status"] != "pruned":
                raise DeploymentError(
                    "unfinished release pruning; resume release-prune first"
                )

    def host_profiles(self):
        result = {}
        selected = self.machine.profiles / "system"
        if selected.is_symlink():
            result[str(selected)] = str(selected.resolve())
        for path in self.machine.profiles.glob("system-*-link"):
            if re.fullmatch(r"system-[0-9]+-link", path.name) and path.is_symlink():
                result[str(path)] = str(path.resolve())
        recovery = self.machine.profiles / "system-profiles"
        for path in recovery.rglob("*") if recovery.exists() else ():
            if path.is_symlink():
                result[str(path)] = str(path.resolve())
        return result

    def release_inventory(self):
        """Audit records survive pruning; only unpruned releases are recovery choices."""
        releases = {}
        for path in (self.machine.state / "transactions").glob("*/status.json"):
            record = read_json(path)
            for key in ("release", "previous"):
                if record.get(key):
                    releases.setdefault(record[key], [])
        for record in self.machine.retirement.trash_records():
            releases.setdefault(record["source_release"], [])
        baseline = self.machine.state / "baseline"
        if (baseline / "release.json").exists():
            releases.setdefault(str(baseline), [])
        for root in (self.machine.state / "roots").glob("*"):
            if root.name.startswith("pending-"):
                candidate = self.machine.state / "candidates" / root.name.removeprefix("pending-")
                if root.is_symlink() and (candidate / "release.json").is_file():
                    releases.setdefault(str(candidate), []).append(root.name)
                continue
            if root.name.startswith("host-"):
                # A direct candidate's transaction root retains its transport
                # closure; the transport is not independently activatable.
                continue
            if root.is_symlink() and (root.resolve() / "release.json").is_file():
                releases.setdefault(str(root.resolve()), []).append(root.name)
        return releases

    def _prune_plan(
        self, release, now=None, manifest=None, early_purge_trash=()
    ) -> PrunePlan:
        release = Path(release).resolve()
        now = time.time() if now is None else now
        manifest = manifest or load_release(release)[0]
        inventory = self.release_inventory()
        if (
            str(release) not in inventory
            and not self.prune_record_path(release).exists()
        ):
            raise DeploymentError("release is not a recorded machine deployment")
        overrides = set(early_purge_trash)
        matching_trash = {
            record["id"]: record
            for record in self.machine.retirement.active_trash().values()
            if record["source_release"] == str(release)
        }
        unknown = overrides - matching_trash.keys()
        if unknown:
            raise DeploymentError(
                "early purge override does not match active trash from release: "
                + ", ".join(sorted(unknown))
            )
        blockers = []
        waived_blockers = []
        from .generation_retention import GenerationRetention
        if GenerationRetention(self.machine).protect_release(release, manifest, now):
            blockers.append("machine generation retention: newest two, weekly anchor or aging successor")
        selected = self.machine.pointer() or {}
        for key in ("release", "previous"):
            if selected.get(key) == str(release):
                blockers.append("selected " + key)
        host = manifest["host"]
        if host in self.machine.running_hosts():
            blockers.append("running or booted host")
        if (self.machine.profiles / "system").is_symlink() and str(
            (self.machine.profiles / "system").resolve()
        ) == host:
            blockers.append("selected Nix system profile")
        profiles = self.host_profiles()
        generations = []
        for path, target in profiles.items():
            if target != host:
                continue
            if Path(path).parent == self.machine.profiles:
                if Path(path).name != "system":
                    generations.append(int(Path(path).name.split("-")[1]))
            else:
                blockers.append("recovery profile " + path)
        keep_path = self.machine.state / "retained-releases.json"
        kept = read_json(keep_path) if keep_path.exists() else []
        if str(release) in kept:
            blockers.append("explicitly retained rollback choice")
        for path in (self.machine.state / "transactions").glob("*/status.json"):
            record = read_json(path)
            if record.get("phase") not in (
                "complete",
                "reboot-required",
            ) and not record.get("recovered_by"):
                if str(release) in (record.get("release"), record.get("previous")):
                    blockers.append("unfinished transaction " + record["id"])
        for record in matching_trash.values():
            if now < record["purge_after"]:
                blocker = "unexpired trash " + record["id"]
                target = waived_blockers if record["id"] in overrides else blockers
                target.append(blocker)
            if record["status"] == "purging":
                blockers.append("unfinished purge " + record["id"])
        # A host can be shared by several releases (e.g. guest-only candidates).
        # Its boot generations remain until the last such release is pruned.
        shared_host = any(
            other != str(release)
            and not self.prune_record_path(other).exists()
            and load_release(other)[0]["host"] == host
            for other in inventory
        )
        roots = list(inventory.get(str(release), []))
        for root in (self.machine.state / "roots").glob("host-*"):
            status = self.machine.state / "transactions" / root.name[5:] / "status.json"
            if status.exists() and read_json(status).get("release") == str(release):
                roots.append(root.name)
        if (
            release == self.machine.state / "baseline"
            and (self.machine.state / "roots/baseline-host").is_symlink()
        ):
            roots.append("baseline-host")
        return {
            "schema": "airlock.machine-release-prune.v1",
            "release": str(release),
            "host": host,
            "manifest": manifest,
            "roots": {
                name: str((self.machine.state / "roots" / name).resolve())
                for name in sorted(set(roots))
            },
            "profile_generations": [] if shared_host else sorted(generations),
            "blockers": sorted(set(blockers)),
            "waived_blockers": sorted(set(waived_blockers)),
        }

    def prune_plan(self, release, early_purge_trash=()) -> PrunePlan | PruneRecord:
        with self.machine.locked():
            path = self.prune_record_path(release)
            if path.exists():
                record = self.read_prune_record(path)
                if record["status"] != "pruned":
                    preview = self._prune_plan(
                        release, manifest=record["manifest"],
                        early_purge_trash=early_purge_trash,
                    )
                    record["blockers"] = preview["blockers"]
                    record["waived_blockers"] = preview["waived_blockers"]
                return record
            return self._prune_plan(release, early_purge_trash=early_purge_trash)

    def list_releases(self):
        with self.machine.locked():
            records = []
            for release in sorted(self.release_inventory()):
                path = self.prune_record_path(release)
                records.append(
                    self.read_prune_record(path)
                    if path.exists()
                    else self._prune_plan(release)
                )
            return {
                "schema": "airlock.machine-release-inventory.v1",
                "releases": records,
            }

    def retain_release(self, release, retain):
        with self.machine.locked():
            self.require_no_pruning()
            self.require_available_release(release)
            release = str(Path(release).resolve())
            if release not in self.release_inventory():
                raise DeploymentError("release is not a recorded machine deployment")
            path = self.machine.state / "retained-releases.json"
            kept = set(read_json(path) if path.exists() else [])
            if retain:
                kept.add(release)
            else:
                kept.discard(release)
            atomic_json(path, sorted(kept))
            return {"release": release, "retain": retain}

    def prune_release(self, release, confirmation, early_purge_trash=()) -> PruneRecord:
        release = str(Path(release).resolve())
        if confirmation != release:
            raise DeploymentError(
                "release pruning confirmation must match the full release path"
            )
        with self.machine.locked():
            return self._prune_release_locked(release, confirmation, early_purge_trash)

    def _prune_release_locked(self, release, confirmation, early_purge_trash=()):
        """Existing journalled pruner; caller already holds the deployment lock."""
        release = str(Path(release).resolve())
        if confirmation != release:
            raise DeploymentError("release pruning confirmation must match the full release path")
        path = self.prune_record_path(release)
        record = self.read_prune_record(path) if path.exists() else None
        if record and record["status"] == "pruned":
            return record
        preview = self._prune_plan(
            release, manifest=record["manifest"] if record else None,
            early_purge_trash=early_purge_trash,
        )
        if preview["blockers"]:
            raise DeploymentError(
                "cannot prune release: " + "; ".join(preview["blockers"])
            )
        if record is None:
            self.require_no_pruning()
            record = dict(preview, status="pruning", started_at=time.time())
            atomic_json(path, record)
        # Verify exact targets on retry. Keep journalled targets even if a
        # previous attempt already removed some profile links or roots.
        for name, target in record["roots"].items():
            root = self.machine.state / "roots" / name
            if root.is_symlink() and str(root.resolve()) != target:
                raise DeploymentError(
                    "release root changed during pruning: " + name
                )
            if root.exists() and not root.is_symlink():
                raise DeploymentError("release root is not a symlink: " + name)
        generations = []
        for number in record["profile_generations"]:
            link = self.machine.profiles / f"system-{number}-link"
            if link.is_symlink():
                if str(link.resolve()) != record["host"]:
                    raise DeploymentError(
                        "Nix profile generation changed during pruning"
                    )
                generations.append(str(number))
        if generations:
            self.machine.command(
                "nix-env",
                "-p",
                str(self.machine.profiles / "system"),
                "--delete-generations",
                *generations,
            )
        # Refresh the boot menu even on retry after profile deletion.
        selected = self.machine.pointer()
        if selected and record["profile_generations"]:
            host = load_release(selected["release"])[0]["host"]
            self.machine.command("airlock-host-store", "install-boot", host, timeout=600)
            self.machine.command("airlock-host-store", "seal-boot")
        for name in record["roots"]:
            (self.machine.state / "roots" / name).unlink(missing_ok=True)
        roots = self.machine.state / "roots"
        if roots.exists():
            fd = os.open(roots, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if not any(
            other != release and not self.prune_record_path(other).exists()
            and load_release(other)[0]["host"] == record["host"]
            for other in self.release_inventory()
        ):
            self.machine.command("airlock-host-store", "retire", record["host"])
        record.update(status="pruned", finished_at=time.time())
        atomic_json(path, record)
        return record
