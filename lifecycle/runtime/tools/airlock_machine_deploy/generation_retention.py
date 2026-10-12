"""Verified whole-machine retention and journalled background pool maintenance."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .common import DeploymentError
from .state import atomic_json, load_release, read_json

POLICY = Path("/etc/airlock/generation-retention.json")
WEEK = 7 * 24 * 60 * 60
POOL = Path("/var/lib/airlock/closure-store")


def generation_key(manifest, topology):
    """Deduplicate candidate/transport records by the complete machine snapshot."""
    payload = {"host": manifest["host"], "setup": manifest["setup"], "topology": topology}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def retention_choices(history, now):
    ordered = sorted(history, key=lambda key: (history[key]["first_verified_at"], key), reverse=True)
    keep = {key: ["newest-two"] for key in ordered[:2]}
    weekly = [key for key in ordered if history[key]["first_verified_at"] <= now - WEEK]
    if ordered:
        anchor = weekly[0] if weekly else ordered[-1]
        keep.setdefault(anchor, []).append("weekly-anchor" if weekly else "oldest-verified-fallback")
        # Preserve a successor while it ages. Otherwise frequent updates prune
        # every intermediate snapshot before it becomes eligible as a weekly
        # anchor, leaving the original anchor retained indefinitely.
        successors = [key for key in ordered
                      if history[key]["first_verified_at"] > history[anchor]["first_verified_at"]]
        if successors:
            successor = min(successors, key=lambda key: (history[key]["first_verified_at"], key))
            keep.setdefault(successor, []).append("aging-weekly-successor")
    return keep


def retained_keys(history, now):
    return set(retention_choices(history, now))


class GenerationRetention:
    def __init__(self, machine):
        self.machine = machine
        self.directory = machine.state / "maintenance"

    def enabled(self):
        return POLICY.exists() and read_json(POLICY).get("enabled") is True

    def history(self):
        path = self.directory / "verified-generations.json"
        history = read_json(path) if path.exists() else {}
        if not isinstance(history, dict) or any(
            not re.fullmatch(r"[0-9a-f]{64}", key)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("release"), str)
            or not Path(entry["release"]).is_absolute()
            or type(entry.get("first_verified_at")) not in (int, float)
            or not math.isfinite(entry["first_verified_at"])
            or entry["first_verified_at"] <= 0
            for key, entry in history.items()
        ):
            raise DeploymentError("invalid verified machine-generation history")
        # Import only historical whole-machine successes. Never promote a
        # prepared candidate, partial apply or reboot-required transaction.
        for path in sorted((self.machine.state / "transactions").glob("*/status.json")):
            record = read_json(path)
            if record.get("phase") != "complete" or not record.get("release"):
                continue
            release = str(Path(record["release"]).resolve())
            if self.machine.releases.prune_record_path(release).exists():
                continue
            manifest, topology = load_release(release)
            if record.get("host_running") != manifest["host"]:
                continue
            stamp = record.get("generation_verified_at", record.get("verified_at", path.stat().st_mtime))
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or stamp <= 0 or stamp > time.time() + 60):
                raise DeploymentError("invalid generation verification timestamp")
            key = generation_key(manifest, topology)
            existing = history.get(key)
            if existing is None or stamp < existing["first_verified_at"]:
                history[key] = {"release": release, "first_verified_at": stamp}
            elif self.machine.releases.prune_record_path(existing["release"]).exists():
                existing["release"] = release
        return {
            key: value for key, value in history.items()
            if not self.machine.releases.prune_record_path(value["release"]).exists()
        }

    def protect_release(self, release, manifest=None, now=None):
        if not self.enabled():
            return False
        if self.machine.releases.prune_record_path(release).exists():
            # An already journalled prune passed policy checks before mutation.
            # Preserve resumability after its transport artifacts disappear;
            # the pruner still rechecks running/recovery/transaction blockers.
            return False
        loaded, topology = load_release(release)
        key = generation_key(manifest or loaded, topology)
        return key in retained_keys(self.history(), time.time() if now is None else now)

    def record_verified(self, record):
        """Called under the deployment lock, after durable successful verification."""
        if not self.enabled() or record.get("phase") != "complete":
            return
        history = self.history()
        manifest, topology = load_release(record["release"])
        if record.get("host_running") != manifest["host"]:
            raise DeploymentError("cannot retain an unbooted machine generation")
        key = generation_key(manifest, topology)
        first = record.setdefault("generation_verified_at", time.time())
        history.setdefault(key, {"release": record["release"], "first_verified_at": first})
        atomic_json(self.directory / "verified-generations.json", history)
        completed = self.directory / "last-cleaned-generation.json"
        if completed.exists() and read_json(completed).get("key") == key:
            return
        # PathExists activates a root-owned systemd worker, including after a
        # reboot or failed cleanup. The marker is removed only after success.
        atomic_json(self.directory / "pending.json", {"key": key, "requested_at": time.time()})

    def plan(self):
        history = self.history()
        choices = retention_choices(history, time.time())
        keep = set(choices)
        prune, protected, unverified = [], [], []
        for release in self.machine.releases.release_inventory():
            if self.machine.releases.prune_record_path(release).exists():
                continue
            manifest, topology = load_release(release)
            key = generation_key(manifest, topology)
            if key not in history:
                unverified.append(release)
                continue
            preview = self.machine.releases._prune_plan(release)
            if key in keep or preview["blockers"]:
                protected.append({"release": release, "blockers": preview["blockers"]})
            else:
                prune.append(release)
        candidates = set()
        for release in prune:
            manifest, _ = load_release(release)
            candidates.update((domain, int(generation))
                              for domain, generation in manifest.get("stagedGenerations", {}).items())
        return {"schema": "airlock.machine-generation-retention.v1", "keep_keys": sorted(keep),
                "keep_generations": [{"key": key, **history[key], "reasons": choices[key]}
                                     for key in sorted(choices)],
                "prune": sorted(prune), "protected": protected, "unverified": unverified,
                "storage_candidates": sorted(candidates)}

    def release_storage_retention(self, candidates):
        """Release admission defaults only; preserve every surviving snapshot."""
        referenced, legacy_systems = set(), set()
        for release in self.machine.releases.release_inventory():
            if self.machine.releases.prune_record_path(release).exists():
                continue
            manifest, topology = load_release(release)
            staged = manifest.get("stagedGenerations", {})
            referenced.update((domain, int(generation)) for domain, generation in staged.items())
            for domain, declaration in topology["domains"].items():
                if domain not in staged:
                    source = declaration.get("backend", {}).get("system", {}).get("source")
                    if source:
                        legacy_systems.add((domain, source))
        changed = []
        for domain, generation in candidates:
            path = POOL / "domains" / domain / "generations" / str(generation) / "manifest.json"
            if not path.exists() or (domain, generation) in referenced:
                continue
            storage = read_json(path)
            retention = storage.get("retention", {})
            # Manual set-retention stamps updated_at. Do not reinterpret it as
            # an automatic admission default or revoke an operator's retain.
            if ((domain, storage["system"]) in legacy_systems
                    or not retention.get("retain") or "updated_at" in retention):
                continue
            self.machine.command("airlock-closure-store", "set-retention", "--pool-root", str(POOL),
                                 "--domain", domain, "--generation", str(generation), "--retain", "false")
            changed.append({"domain": domain, "generation": generation})
        return changed

    def run(self):
        """Serialize policy, existing resumable pruning and pool GC with deployment."""
        with self.machine.locked():
            if not self.enabled():
                raise DeploymentError("machine generation retention is disabled")
            pending = self.directory / "pending.json"
            if not pending.exists():
                return {"status": "idle"}
            request = read_json(pending)
            selected = self.machine.pointer()
            if not selected:
                raise DeploymentError("no selected machine generation")
            status = read_json(self.machine.state / "transactions" / selected["transaction"] / "status.json")
            manifest, topology = load_release(selected["release"])
            if (status.get("phase") != "complete"
                    or str(Path("/run/current-system").resolve()) != manifest["host"]
                    or generation_key(manifest, topology) != request["key"]):
                raise DeploymentError("cleanup must wait for the selected generation's verification")
            journal = self.directory / "latest.json"
            previous = read_json(journal) if journal.exists() else {}
            result = {"status": "running", "started_at": time.time(), "request": request}
            before = os.statvfs(self.machine.state)
            result["available_before_bytes"] = before.f_bavail * before.f_frsize
            try:
                # Finish this worker's interrupted prune before planning anew.
                if previous.get("pruning"):
                    target = previous["pruning"]
                    self.machine.releases._prune_release_locked(target, target)
                result["plan"] = self.plan()
                # A crash may occur after pruning a canonical candidate but
                # before its duplicate transport roots. Resume that journalled
                # scope even if the candidate no longer enters verified history.
                if previous.get("status") != "passed":
                    retry = previous.get("plan", {}).get("prune", [])
                    for target in retry:
                        if (not self.machine.releases.prune_record_path(target).exists()
                                and not self.machine.releases._prune_plan(target)["blockers"]):
                            result["plan"]["prune"].append(target)
                    result["plan"]["prune"] = sorted(set(result["plan"]["prune"]))
                result["storage_candidates"] = sorted({
                    tuple(candidate) for candidate in result["plan"]["storage_candidates"]
                } | {tuple(candidate) for candidate in previous.get("storage_candidates", [])})
                atomic_json(journal, result)
                result["pruned"] = []
                for target in result["plan"]["prune"]:
                    result["pruning"] = target
                    atomic_json(journal, result)
                    self.machine.releases._prune_release_locked(target, target)
                    result["pruned"].append(target)
                    result.pop("pruning")
                    atomic_json(journal, result)
                result["storage_released"] = self.release_storage_retention(result["storage_candidates"])
                atomic_json(journal, result)
                result["gc"] = json.loads(self.machine.command(
                    "airlock-closure-store", "gc", "--pool-root", str(POOL),
                    "--keep-generations", "2", timeout=3600,
                ))
                after = os.statvfs(self.machine.state)
                result.update(status="passed", finished_at=time.time(),
                              available_after_bytes=after.f_bavail * after.f_frsize,
                              available_change_bytes=after.f_bavail * after.f_frsize - result["available_before_bytes"])
                atomic_json(journal, result)
                atomic_json(self.directory / "verified-generations.json", self.history())
                atomic_json(self.directory / "last-cleaned-generation.json", request)
                pending.unlink()
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return result
            except Exception as error:
                result.update(status="failed", error=str(error))
                # Preserve the interrupted prune target for an idempotent retry.
                if previous.get("pruning") and "plan" not in result:
                    result["pruning"] = previous["pruning"]
                atomic_json(journal, result)
                raise
