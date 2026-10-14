"""Root-owned pre-build authorizations, checked again before activation."""
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from .common import DeploymentError
from .state import atomic_json, load_release, read_json

MARKER = Path("/etc/airlock/review-required")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
TOKEN = re.compile(r"[0-9a-f]{64}")


def scope(value):
    if not isinstance(value, dict) or set(value) != {"allowed_vms", "allow_reboot"}:
        raise DeploymentError("invalid approval scope")
    names = value["allowed_vms"]
    if (not isinstance(names, list) or not 0 < len(names) <= 4096
            or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in names)
            or type(value["allow_reboot"]) is not bool):
        raise DeploymentError("invalid approval scope")
    return {"allowed_vms": sorted(set(names)), "allow_reboot": value["allow_reboot"]}


def check_scope(allowed, plan):
    allowed = scope(allowed)
    affected = set()
    for key in ("changed", "stop", "start", "retire", "restore", "stage", "affected"):
        names = plan.get(key, [])
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise DeploymentError("invalid deployment plan")
        affected.update(names)
    for item in plan.get("private_copies", []):
        if not isinstance(item, dict):
            raise DeploymentError("invalid private copy plan")
        source = item.get("source", {})
        if not isinstance(source, dict) or not isinstance(source.get("domain"), str) or not isinstance(item.get("target_name"), str):
            raise DeploymentError("invalid private copy plan")
        affected.update([source["domain"], item["target_name"]])
    outside = affected - set(allowed["allowed_vms"])
    if outside:
        raise DeploymentError("scope_exceeded: VM impact outside approval: " + ", ".join(sorted(outside)))
    if plan.get("host_reboot_required") is not False and not allowed["allow_reboot"]:
        raise DeploymentError("scope_exceeded: system reboot was not permitted")


class Reviews:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.root = coordinator.state / "reviews"

    def path(self, identity):
        if not isinstance(identity, str) or not TOKEN.fullmatch(identity):
            raise DeploymentError("invalid review request ID")
        return self.root / "requests" / (identity + ".json")

    def baseline(self):
        manifest, topology = load_release(self.coordinator.baseline())
        revision = manifest["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise DeploymentError("deployed source baseline is unavailable for review")
        from .coordinator import boot_id
        state = {"selection": self.coordinator.pointer(), "boot_id": boot_id(),
                 "host": str(Path("/run/current-system").resolve()), "revision": revision}
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        return revision, fingerprint, sorted(topology["domains"])

    def context(self, request):
        if set(request) != {"op", "revision", "lock_hash", "family", "target"}:
            raise DeploymentError("unexpected review context fields")
        if (not re.fullmatch(r"[0-9a-f]{40}", str(request["revision"]))
                or not TOKEN.fullmatch(str(request["lock_hash"]))
                or request["family"] not in ("machine", "guest")
                or (request["family"] == "guest" and not NAME.fullmatch(str(request["target"])))
                or (request["family"] == "machine" and request["target"] is not None)):
            raise DeploymentError("invalid review context")
        baseline, state, names = self.baseline()
        binding = {key: request[key] for key in ("revision", "lock_hash", "family", "target")}
        # Reuse the same human authorization for plan followed by apply, never
        # for a different source, lock, target, selected baseline or boot.
        for path in sorted((self.root / "requests").glob("*.json"), reverse=True):
            saved = read_json(path)
            if saved["binding"] == binding and saved["baseline_state"] == state:
                return saved
        identity = uuid.uuid4().hex + uuid.uuid4().hex
        record = {"request_id": identity, "binding": binding, "baseline": baseline,
                  "baseline_state": state, "scope": {"allowed_vms": names, "allow_reboot": False},
                  "status": "awaiting_approval", "created_at": time.time()}
        atomic_json(self.path(identity), record)
        return record

    def decide(self, request):
        if set(request) != {"op", "request_id", "decision", "scope", "review_fingerprint"}:
            raise DeploymentError("unexpected decision fields")
        path = self.path(request["request_id"])
        record = read_json(path)
        if record["status"] != "awaiting_approval":
            raise DeploymentError("review has already been decided")
        decision = request["decision"]
        if decision not in ("approve", "reject", "cancelled"):
            raise DeploymentError("invalid human decision")
        if decision == "approve" and self.baseline()[1] != record["baseline_state"]:
            raise DeploymentError("stale_review: deployed baseline changed before approval")
        if not TOKEN.fullmatch(str(request["review_fingerprint"])):
            raise DeploymentError("invalid source review fingerprint")
        record.update(scope=scope(request["scope"]), review_fingerprint=request["review_fingerprint"],
                      status={"approve": "approved", "reject": "rejected", "cancelled": "cancelled"}[decision],
                      decided_at=time.time())
        atomic_json(path, record)
        return record

    def require(self, identity, revision, lock_hash):
        record = read_json(self.path(identity))
        if record["status"] != "approved":
            raise DeploymentError("review is not approved: " + record["status"])
        if (record["binding"]["revision"], record["binding"]["lock_hash"]) != (revision, lock_hash):
            raise DeploymentError("source differs from approved candidate")
        if record["baseline_state"] != self.baseline()[1]:
            raise DeploymentError("stale_review: deployed baseline changed")
        return record

    def bind(self, identity, release):
        manifest, _ = load_release(release)
        record = self.require(identity, manifest["revision"], manifest["lockHash"])
        if record["binding"]["family"] == "guest" and manifest.get("guestAdmission", {}).get("domain") != record["binding"]["target"]:
            raise DeploymentError("guest approval differs from deployment target")
        plan = self.coordinator.inspect(release)
        try:
            check_scope(record["scope"], plan)
        except DeploymentError:
            record.update(status="scope_exceeded", plan=plan)
            atomic_json(self.path(identity), record)
            raise
        binding = {"request_id": identity, "release": str(Path(release).resolve()),
                   "revision": manifest["revision"], "lock_hash": manifest["lockHash"]}
        key = hashlib.sha256(binding["release"].encode()).hexdigest()
        atomic_json(self.root / "bindings" / (key + ".json"), binding)
        return {"request_id": identity, "status": "approved", "plan": plan}

    def activation(self, release, manifest, plan, recover=False):
        if not MARKER.exists() or recover:
            return
        key = hashlib.sha256(str(Path(release).resolve()).encode()).hexdigest()
        path = self.root / "bindings" / (key + ".json")
        if not path.exists():
            raise DeploymentError("activation requires a human-approved source and scope")
        binding = read_json(path)
        record = read_json(self.path(binding["request_id"]))
        if (binding["revision"], binding["lock_hash"]) != (manifest["revision"], manifest["lockHash"]):
            raise DeploymentError("approved release identity changed")
        if record["status"] not in ("approved", "deployment_started"):
            raise DeploymentError("review is not approved")
        if not recover:
            self.require(binding["request_id"], manifest["revision"], manifest["lockHash"])
        try:
            check_scope(record["scope"], plan)
        except DeploymentError:
            record.update(status="scope_exceeded", plan=plan)
            atomic_json(self.path(binding["request_id"]), record)
            raise
        record.update(status="deployment_started", release=str(Path(release).resolve()), started_at=time.time())
        atomic_json(self.path(binding["request_id"]), record)

    def control(self, request):
        if not isinstance(request, dict):
            raise DeploymentError("invalid review control request")
        if request.get("op") == "status" and set(request) == {"op", "request_id"}:
            return read_json(self.path(request["request_id"]))
        if request.get("op") == "context":
            return self.context(request)
        if request.get("op") == "decide":
            return self.decide(request)
        if request.get("op") == "check" and set(request) == {"op", "request_id", "revision", "lock_hash"}:
            return self.require(request["request_id"], request["revision"], request["lock_hash"])
        raise DeploymentError("unsupported review operation")
