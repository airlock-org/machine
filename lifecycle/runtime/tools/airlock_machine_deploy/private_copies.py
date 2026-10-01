"""Explicit, journalled private-volume copies for replacing a retired VM.

Qubes performs the independent storage clone. A completed receipt prevents
subsequent apply/resume operations from overwriting the replacement's data.
"""

from .common import DeploymentError
from .state import atomic_json, read_json


class PrivateVolumeCopies:
    def __init__(self, machine):
        self.machine = machine

    def app(self):
        import qubesadmin

        return qubesadmin.Qubes()

    def path(self, target):
        return self.machine.state / "private-copies" / f"{target}.json"

    @staticmethod
    def identity(vm):
        volume = vm.volumes["private"]
        if not volume.rw or not volume.save_on_stop or volume.snap_on_start or volume.source:
            raise DeploymentError(f"{vm.name}: copy requires independent persistent private storage")
        return {"domain": vm.name, "uuid": str(vm.uuid),
                "pool": volume.pool, "vid": volume.vid}

    def inspect(self, copies, old, new):
        if not copies:
            return []
        app = self.app()
        result = []
        for target, source in sorted(copies.items()):
            if source not in old["domains"] or target in old["domains"]:
                raise DeploymentError("private copy must replace an old VM with a new VM")
            for topology, name in ((old, source), (new, target)):
                domain = topology["domains"][name]
                if (domain.get("class") != "StandaloneVM"
                        or domain["backend"].get("type") != "nix-closure"
                        or not domain["backend"].get("state", {}).get("persistent")):
                    raise DeploymentError(f"{name}: unsupported private copy backend")
            source_identity = self.identity(app.domains[source])
            path = self.path(target)
            receipt = read_json(path) if path.exists() else None
            if receipt and (receipt.get("source") != source_identity
                            or receipt.get("target_name") != target
                            or receipt.get("target_config") != new["domains"][target]
                            or receipt.get("status") not in {"planned", "copying", "complete"}):
                raise DeploymentError(f"{target}: private copy receipt differs from requested replacement")
            if target in app.domains:
                if not receipt:
                    raise DeploymentError(f"{target}: refuse to overwrite an existing VM")
                identity = self.identity(app.domains[target])
                if receipt.get("target") and receipt["target"] != identity:
                    raise DeploymentError(f"{target}: private copy target identity changed")
                if app.domains[target].is_running() and receipt["status"] != "complete":
                    raise DeploymentError(f"{target}: incomplete copy target is running")
            result.append({"source": source_identity, "target_name": target,
                           "target_config": new["domains"][target],
                           "status": receipt["status"] if receipt else "planned"})
        return result

    def prepare(self, entries):
        # Persist intent before setup can create the new VM. Preflight has
        # already rejected an unrelated VM under the destination name.
        for entry in entries:
            path = self.path(entry["target_name"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(path, entry)

    def apply(self, entries):
        if not entries:
            return
        app = self.app()
        for entry in entries:
            path = self.path(entry["target_name"])
            receipt = read_json(path)
            source = app.domains[entry["source"]["domain"]]
            target = app.domains[entry["target_name"]]
            target_identity = self.identity(target)
            if (target_identity["pool"], target_identity["vid"]) == (
                    receipt["source"]["pool"], receipt["source"]["vid"]):
                raise DeploymentError("private copy source and target share storage")
            if self.identity(source) != receipt["source"] or (
                    receipt.get("target") and receipt["target"] != target_identity):
                raise DeploymentError("private copy VM or volume identity changed")
            if receipt["status"] == "complete":
                continue
            if source.is_running() or target.is_running():
                raise DeploymentError("private copy requires both VMs stopped")
            if target.volumes["private"].size < source.volumes["private"].size:
                raise DeploymentError("private copy target is smaller than source")
            receipt.update(target=target_identity, status="copying")
            atomic_json(path, receipt)
            target.volumes["private"].clone(source.volumes["private"])
            receipt["status"] = "complete"
            atomic_json(path, receipt)
