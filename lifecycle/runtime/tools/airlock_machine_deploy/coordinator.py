#!/usr/bin/env python3
"""Root-owned, journalled whole-machine Nix deployment coordinator.

Transport and configuration evaluation deliberately live outside this module.
Qubes remains responsible for VM lifecycle, devices, policy and storage.
"""

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
import time

from .common import (
    SCHEMA,
    GENERATION_INFO_SCHEMA,
    DEFAULT_TRASH_RETENTION_DAYS,
    RETIRED_POLICY,
    DeploymentError,
)
from .planning import plan
from .diagnostics import DeploymentDiagnostics
from .releases import ReleaseManager
from .retirement import RetirementManager
from .private_copies import PrivateVolumeCopies
from .state import read_json, atomic_json, load_release
from .validation import validate_staged_generation, validate_admission_receipt
from .models import DeploymentPhase, DeploymentPlan, DeploymentRecord, Selection
from .boot import BootConfiguration, digest
from .gui_reconnect import GUIReconnect

STATE = Path("/var/lib/airlock/machine")


def run(*args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, **kwargs
    ).stdout.strip()


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def active_profile():
    marker = Path("/etc/airlock/machine-profile")
    if marker.exists():
        return marker.read_text().strip()
    raise DeploymentError("this boot profile does not support machine deployment")


class Coordinator:
    def __init__(self, state=STATE, command=run):
        self.state = Path(state)
        self.command = command
        self.profiles = Path("/nix/var/nix/profiles")
        self.retired_policy = RETIRED_POLICY
        self.retirement = RetirementManager(self)
        self.private_copies = PrivateVolumeCopies(self)
        self.releases = ReleaseManager(self)

    def running_hosts(self):
        return {
            str(Path(p).resolve())
            for p in ("/run/current-system", "/run/booted-system")
        }

    def volume_inventory(self):
        import qubesadmin

        app = qubesadmin.Qubes()
        return {
            vm.name: {
                name: {
                    key: getattr(volume, key)
                    for key in (
                        "pool",
                        "vid",
                        "source",
                        "rw",
                        "save_on_stop",
                        "snap_on_start",
                    )
                }
                for name, volume in vm.volumes.items()
            }
            for vm in app.domains
            if vm.name != "dom0"
        }

    @contextlib.contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("another machine deployment is running") from exc
            yield lock

    def pointer(self) -> Selection | None:
        path = self.state / "selection.json"
        return read_json(path) if path.exists() else None

    def running(self, timeout=None):
        text = self.command(
            "qvm-ls", "--raw-data", "--fields", "NAME,STATE", timeout=timeout
        )
        return {
            line.split("|")[0]
            for line in text.splitlines()
            if line.endswith("|Running") and not line.startswith("dom0|")
        }

    def pin(self, name, path):
        root = self.state / "roots" / name
        root.parent.mkdir(exist_ok=True)
        self.command("nix-store", "--add-root", str(root), "--realise", str(path))

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

    def inspect(self, release) -> DeploymentPlan:
        self.releases.require_available_release(release)
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
            trashed=self.retirement.active_trash(),
        )
        result["retirement"] = self.retirement.retirement_details(
            old,
            result["retire"],
            desired.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS),
        )
        result["host_reboot_required"] = (
            str(Path("/run/current-system").resolve()) != desired["host"]
        )
        if desired.get("privateVolumeCopies"):
            result["private_copies"] = self.private_copies.inspect(
                desired["privateVolumeCopies"], old, new)
        result["revision"] = desired["revision"]
        return result

    def preparation_state(self):
        """Cheap mutable-state guard, checked again under the apply lock."""
        latest = self.state / "latest.json"
        live = self.command("nix-store", "--gc", "--print-live")
        payload = {"selection": self.pointer(),
                   "latest": read_json(latest) if latest.exists() else None,
                   "running": sorted(self.running()),
                   "host_live_paths": sorted(live.splitlines()),
                   "booted_host": str(Path("/run/current-system").resolve())}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def check_prepared(self, release):
        with self.locked():
            self.releases.require_no_pruning()
            self.releases.require_available_release(release)
            manifest, topology = load_release(release)
            for name, generation in manifest.get("stagedGenerations", {}).items():
                validate_staged_generation(self.generation_info(name, generation), name,
                    generation, topology["domains"][name]["backend"], manifest)
            record = json.loads(self.command("airlock-host-store", "validate", manifest["host"]))
            if record != manifest.get("hostPool"):
                raise DeploymentError("prepared host view differs from release")
            return {"plan": self.inspect(release), "state": self.preparation_state()}

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

    def machine_candidate(self, release, receipts):
        """Bind a controller release to exact, independently checked pool generations."""
        with self.locked():
            manifest, topology = load_release(release)
            if manifest.get("guestTransport") != "direct-pool-v1":
                raise DeploymentError("release does not use direct pool admission")
            domains = {
                name: domain["backend"]
                for name, domain in topology["domains"].items()
                if domain["backend"]["type"] == "nix-closure"
            }
            if not isinstance(receipts, dict) or set(receipts) != set(domains):
                raise DeploymentError("admission receipts differ from release guests")
            roots = read_json(Path(release) / "root-artifacts.json")
            if not isinstance(roots, dict) or set(roots) != set(domains):
                raise DeploymentError("root artifact catalog differs from release guests")
            manifest = dict(manifest, guestRootArtifacts=roots, guestProvenance={})
            staged = {}
            timings = {}
            started = time.monotonic()
            for name, backend in domains.items():
                receipt = receipts[name]
                domain, generation, revision, lock_hash = validate_admission_receipt(receipt)
                if domain != name:
                    raise DeploymentError("admission domain differs from release")
                if (revision, lock_hash) != (manifest["revision"], manifest["lockHash"]) and not receipt.get("generation_reused"):
                    raise DeploymentError("admission provenance differs from release")
                manifest["guestProvenance"][name] = {"revision": revision, "lockHash": lock_hash}
                info = self.generation_info(name, generation)
                validate_staged_generation(info, name, generation, backend, manifest)
                if info.get("approval", {}).get("approved_by") != "sys-ctrl":
                    raise DeploymentError("whole-machine admission requires sys-ctrl")
                for key in ("manifest", "system", "root_source", "root_artifact", "kernel_name"):
                    if info.get(key) != receipt.get(key):
                        raise DeploymentError("admission receipt identity mismatch")
                if info.get("store", {}).get("closure_hash") != receipt.get("closure_hash"):
                    raise DeploymentError("admission receipt closure mismatch")
                staged[name] = generation
            # Verify and retain dom0's objects before a candidate is reviewable.
            # This also publishes an approved controller base for its next
            # stopped setup; the running controller keeps its existing view.
            timings["guest_binding_seconds"] = time.monotonic() - started
            started = time.monotonic()
            publication = json.loads(self.command("airlock-host-store", "publish", manifest["host"]))
            timings["host_publication_seconds"] = time.monotonic() - started
            timings.update(publication.pop("timings", {}))
            host_pool = publication
            manifest = dict(manifest, hostPool=host_pool)
            manifest = dict(manifest, stagedGenerations=staged, sourceRelease=str(release))
            identity = hashlib.sha256(json.dumps(
                {"release": manifest, "topology": topology}, sort_keys=True
            ).encode()).hexdigest()
            candidate = self.state / "candidates" / identity
            expected = {"release.json": manifest, "topology.json": topology,
                        "staged.json": staged, "receipts.json": {
                            name: {key: receipt.get(key) for key in (
                                "domain", "generation", "manifest", "system", "root_source", "root_artifact",
                                "kernel_name", "closure_hash", "source_revision", "lock_hash")}
                            for name, receipt in receipts.items()}}
            if candidate.exists():
                if any(read_json(candidate / key) != value for key, value in expected.items()):
                    raise DeploymentError("existing machine candidate differs")
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                temporary = candidate.parent / f".{identity}.new-{time.time_ns()}"
                temporary.mkdir()
                try:
                    for key, value in expected.items():
                        atomic_json(temporary / key, value)
                    temporary.rename(candidate)
                    fd = os.open(candidate.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)
            self.binding_timings = timings
            return candidate

    def guest_candidate(self, receipt):
        domain, generation, revision, lock_hash = validate_admission_receipt(receipt)

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
            "root_artifact": receipt.get("root_artifact"),
            "kernel_name": receipt.get("kernel_name"),
            "closure_hash": receipt.get("closure_hash"),
        }
        info_identity = {
            "domain": info.get("domain"),
            "generation": info.get("generation"),
            "manifest": info.get("manifest"),
            "system": info.get("system"),
            "root_source": info.get("root_source"),
            "root_artifact": info.get("root_artifact"),
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
        candidate_backend["root"]["source"] = info["root_source"]
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
            # A guest update changes no domain identities. Migration actions
            # belong to the transition into the baseline, not later releases.
            retireDomains=[],
            privateVolumeCopies={},
            stagedGenerations={**current_release.get("stagedGenerations", {}),
                               domain: generation},
            guestRootArtifacts={**current_release.get("guestRootArtifacts", {}),
                                domain: info["root_artifact"]},
            guestProvenance={**current_release.get("guestProvenance", {}),
                             domain: {"revision": revision, "lockHash": lock_hash}},
            guestAdmission={
                "domain": domain,
                "generation": generation,
                "manifest": info["manifest"],
            },
        )
        validate_staged_generation(
            info, domain, generation, candidate_backend, candidate_manifest
        )
        if (backend["system"]["source"] == info["system"]
                and backend["root"]["source"] == info["root_source"]):
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
            "staged.json": candidate_manifest["stagedGenerations"],
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
        """Select the controller for live requests; boot hooks are declarative."""
        manifest, _ = load_release(release)
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

    def stage(self, release, topology, names, directory):
        release_manifest, _ = load_release(release)
        preferred = release_manifest.get("stagedGenerations", {})
        if release_manifest.get("guestTransport") == "direct-pool-v1":
            expected = {name for name, domain in topology["domains"].items()
                        if domain["backend"]["type"] == "nix-closure"}
            if set(preferred) != expected:
                raise DeploymentError("direct release is missing admitted generations")
            names = sorted(set(names) | expected)
        unexpected = set(preferred) - {
            name for name, domain in topology["domains"].items()
            if domain["backend"]["type"] == "nix-closure"
        }
        if unexpected:
            raise DeploymentError(
                "staged generation is not part of this plan: "
                + ", ".join(sorted(unexpected))
            )
        # Setup reconciles every domain, including unchanged guests whose
        # original build directories may no longer exist. Validate and pass
        # their retained generations without adding them to the restart plan.
        names = sorted(set(names) | set(preferred))
        staged = {}
        for name in names:
            backend = topology["domains"][name]["backend"]
            store = backend["store"]
            artifact = store["source"]
            if name in preferred:
                generation = preferred[name]
                info = self.generation_info(name, generation)
                validate_staged_generation(
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
                    "usr-assistant",
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

    def handoff_network_pci(self, handoff, *, dry_run=False, allow_unassigned=False):
        """Release the old driver's network NICs before setup assigns sys-net."""
        import qubesadmin

        previous = handoff["from"]
        target = handoff["to"]
        if {previous, target} not in (
            {"sys-agent", "sys-net"}, {"usr-assistant", "sys-net"}
        ):
            raise DeploymentError("unsupported automatic network PCI handoff")
        app = qubesadmin.Qubes()
        network_ports = set()
        for dev in app.domains["dom0"].devices["pci"]:
            try:
                if repr(dev.interfaces[0]).startswith("p02"):
                    network_ports.add(dev.port_id)
            except (AttributeError, IndexError, TypeError):
                continue
        if not network_ports:
            raise DeploymentError(
                "network PCI handoff found no dom0 network controllers"
            )
        assignments = self.command("qvm-device", "pci", "list", "--assignments", "dom0")
        owned = set()
        for line in assignments.splitlines():
            fields = line.split()
            if not fields or not fields[0].startswith("dom0:"):
                continue
            port = fields[0].split(":", 1)[1]
            if port not in network_ports:
                continue
            owners = set(re.findall(r"\*([A-Za-z0-9_-]+) \(", line))
            foreign = owners - {previous, target}
            if foreign:
                raise DeploymentError(
                    f"network PCI controller {port} is assigned to "
                    + ", ".join(sorted(foreign))
                )
            if previous in owners and target in owners:
                raise DeploymentError(f"network PCI controller {port} has both owners")
            owned.update(owners)
            if f"*{previous} (" in line:
                if not dry_run:
                    self.command(
                        "qvm-device", "pci", "unassign", previous, f"dom0:{port}"
                    )
        if not owned and not allow_unassigned:
            raise DeploymentError(
                "network PCI handoff found no assigned network controller"
            )

    def verify_storage(self, name, backend):
        generation = self.command("qvm-features", name, "airlock-store-generation")
        if not generation.isdecimal() or int(generation) < 1:
            raise DeploymentError(f"{name}: invalid selected store generation")
        info = self.generation_info(name, int(generation))
        expected = {
            "system": backend["system"]["source"],
            "root_source": backend["root"]["source"],
            "kernel_name": backend["kernel"]["name"],
            "closure_source": backend["store"]["source"],
        }
        if info.get("selected") is not True or any(
            info.get(key) != value for key, value in expected.items()
        ):
            raise DeploymentError(f"{name}: selected pool generation differs from release boot identity")

    def verify(self, topology, expected_running):
        observed = {}
        for name, domain in topology["domains"].items():
            expected = domain["backend"]["system"]["source"]
            selected = self.command("qvm-features", name, "airlock-system-source")
            if selected != expected:
                raise DeploymentError(
                    f"{name}: selected system {selected!r}, expected {expected}"
                )
            self.verify_storage(name, domain["backend"])
            actual = None
            if name in expected_running:
                actual = self.wait_boot_guest(name, expected)
            observed[name] = {"selected": selected, "running": actual}
        return observed

    def expected_running(self, topology, record):
        # Apply preserves VMs that were manually running. That expectation ends
        # at reboot, when the declared autostart policy (and dependencies) wins.
        if record["boot_id"] == boot_id():
            required = set(record["plan"]["start"])
        else:
            required = set(plan(topology, topology, set())["start"])
        # Also verify manually started guests without requiring them next boot.
        return required | (self.running() & set(topology["domains"]))

    def apply(self, release, recover=False, expected_state=None) -> DeploymentRecord:
        release = Path(release).resolve()
        self.releases.require_available_release(release)
        manifest, topology = load_release(release)
        with self.locked():
            self.releases.require_no_pruning()
            self.releases.require_available_release(release)
            if expected_state is not None and self.preparation_state() != expected_state:
                raise DeploymentError("prepared candidate is stale; run machine-plan again")
            latest = self.state / "latest.json"
            if latest.exists() and not recover:
                existing = read_json(latest)
                if existing["phase"] in ("complete", "reboot-required") and existing[
                    "release"
                ] == str(release):
                    self.verify(topology, self.expected_running(topology, existing))
                    self.retirement.enforce_trash()
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
                trashed=self.retirement.active_trash(),
                retain_removed=rollback,
            )
            if changes.get("pci_handoff"):
                self.handoff_network_pci(
                    changes["pci_handoff"],
                    dry_run=True,
                    allow_unassigned=bool(interrupted),
                )
            changes["stop"] = [name for name in changes["stop"] if name in runtime]
            from .review import Reviews
            approval_plan = dict(changes, host_reboot_required=(
                str(Path("/run/current-system").resolve()) != manifest["host"]))
            Reviews(self).activation(release, manifest, approval_plan, recover=recover)
            copy_plan = self.private_copies.inspect(
                manifest.get("privateVolumeCopies", {}), old, topology)
            if copy_plan:
                changes["private_copies"] = copy_plan
            changes["retirement"] = self.retirement.retirement_details(
                old,
                changes["retire"],
                manifest.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS),
            )
            active_trash = self.retirement.active_trash()
            restore_plan = []
            for name in changes["restore"]:
                trash = active_trash[name]
                self.retirement.validate_restore(name, topology["domains"][name], trash)
                restore_plan.append(trash)
            gui = GUIReconnect(self)
            gui_preserved = gui.snapshot(
                changes.get("gui_reconnect", {}),
                interrupted.get("gui_preserved", {}) if interrupted and not rollback else None,
            )
            transaction = str(time.time_ns())
            directory = self.state / "transactions" / transaction
            directory.mkdir(parents=True)
            record: DeploymentRecord = {
                "id": transaction,
                "boot_id": boot_id(),
                "release": str(release),
                "previous": str(baseline),
                "revision": manifest["revision"],
                "plan": changes,
                "phase": "preflight",
                "gui_preserved": gui_preserved,
            }

            original_command = self.command
            diagnostics = (
                DeploymentDiagnostics(
                    directory, sorted(set(topology["domains"]) | set(old["domains"]))
                )
                if original_command is run
                else None
            )

            def phase(value: DeploymentPhase, **extra) -> None:
                record.update(phase=value, **extra)
                atomic_json(directory / "status.json", record)
                atomic_json(latest, record)
                if value == "complete":
                    self.record_generation_verification(record)
                if diagnostics:
                    diagnostics.event("phase", phase=value)
                print(
                    json.dumps({"transaction": transaction, "phase": value}), flush=True
                )

            phase("staging")
            try:
                if diagnostics:
                    self.command = diagnostics.execute
                    diagnostics.start()
                if str(release).startswith("/nix/store/"):
                    self.pin(transaction, release)
                else:
                    self.pin("host-" + transaction, manifest.get("sourceRelease", manifest["host"]))
                # An applied candidate is now protected by its transaction root.
                (self.state / "roots" / ("pending-" + release.name)).unlink(missing_ok=True)
                self.command("airlock-host-store", "validate" if manifest.get("hostPool") else "publish", manifest["host"])
                self.install_boot_hook(release, baseline)
                self.stage(release, topology, changes["stage"], directory)
                # Complete fallible boot preparation before disrupting guests.
                # Installation and sealing preserve the previous boot default.
                phase("host-boot-preparation")
                self.command("airlock-host-store", "protect-boot")
                self.command(
                    "nix-env", "-p", "/nix/var/nix/profiles/system", "--set", manifest["host"],
                )
                self.command("airlock-host-store", "install-boot", manifest["host"], timeout=600)
                self.command("airlock-host-store", "seal-boot", "--keep-default")
                trash_plan = [
                    self.retirement.prepare_retirement(
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
                self.command("airlock-host-store", "checkpoint-machine")
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
                    if diagnostics:
                        diagnostics.event(
                            "vm-stop",
                            domain=name,
                            directly_changed=name in changes["changed"],
                            plan=changes,
                        )
                        diagnostics.capture(
                            name + "-pci-before-stop.txt",
                            ["qvm-device", "pci", "list", "--assignments", name],
                        )
                        diagnostics.capture(
                            name + "-state-before-stop.txt",
                            [
                                "qvm-ls",
                                "--raw-data",
                                "--fields",
                                "NAME,STATE,XID",
                                name,
                            ],
                        )
                    if name in changes.get("gui_reconnect", {}):
                        gui.command = self.command
                        gui.shutdown(name, changes["gui_reconnect"][name])
                    else:
                        self.command(
                            "qvm-shutdown", "--wait", "--timeout", "120", name, timeout=150
                        )
                phase("retiring")
                retired = [self.retirement.retire(entry) for entry in trash_plan]
                phase("configuring")
                self.private_copies.prepare(copy_plan)
                if changes.get("pci_handoff"):
                    self.handoff_network_pci(
                        changes["pci_handoff"], allow_unassigned=bool(interrupted)
                    )
                self.setup(release, directory)
                if copy_plan:
                    phase("copying-private")
                    self.private_copies.apply(copy_plan)
                restored = [
                    self.retirement.restore(entry, release, transaction)
                    for entry in restore_plan
                ]
                phase("starting")
                for name in changes["start"]:
                    if name not in self.running():
                        if diagnostics:
                            diagnostics.event("vm-start", domain=name)
                            diagnostics.capture(
                                name + "-pci-before-start.txt",
                                ["qvm-device", "pci", "list", "--assignments", name],
                            )
                        self.command("qvm-start", name, timeout=240)
                phase("verifying")
                observed = self.verify(topology, set(changes["start"]))
                if gui_preserved:
                    phase("gui-reconnecting")
                    gui.command = self.command
                    record["gui_reconnected"] = gui.verify(gui_preserved)
                # A verified full apply authorizes reuse on subsequent boots.
                # Interrupted transactions still block boot independently.
                if Path("/etc/airlock/split-startup").exists():
                    BootConfiguration(self).record(
                        digest({"topology": topology, "setup": manifest["setup"],
                                "generations": manifest.get("stagedGenerations", {})}),
                        topology, list(topology["domains"]),
                    )
                phase("host-boot-selection")
                self.command("airlock-host-store", "bind-machine", manifest["host"])
                self.command("airlock-host-store", "select", manifest["host"])
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
                if interrupted:
                    interrupted["recovered_by"] = transaction
                    atomic_json(
                        self.state / "transactions" / interrupted["id"] / "status.json",
                        interrupted,
                    )
            except Exception as exc:
                phase("failed", error=str(exc), failed_phase=record["phase"])
                raise
            finally:
                self.command = original_command
                if diagnostics:
                    diagnostics.close()
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

    def record_generation_verification(self, record):
        """Persist success before queuing independent maintenance; never prune here."""
        from .generation_retention import GenerationRetention
        if record["phase"] == "complete":
            record.setdefault("generation_verified_at", time.time())
            record["verified_at"] = time.time()
            directory = self.state / "transactions" / record["id"]
            atomic_json(directory / "status.json", record)
            atomic_json(self.state / "latest.json", record)
            try:
                GenerationRetention(self).record_verified(record)
                record.pop("maintenance_error", None)
            except (OSError, ValueError, DeploymentError) as error:
                record["maintenance_error"] = str(error)
            atomic_json(directory / "status.json", record)
            atomic_json(self.state / "latest.json", record)

    def verify_selected(self) -> DeploymentRecord:
        with self.locked():
            selected = self.pointer()
            if not selected:
                raise DeploymentError("no selected deployment")
            directory = self.state / "transactions" / selected["transaction"]
            record = read_json(directory / "status.json")
            if record["phase"] not in ("complete", "reboot-required"):
                raise DeploymentError("unfinished deployment; resume or rollback first")
            manifest, topology = load_release(selected["release"])
            record["observed"] = self.verify(
                topology, self.expected_running(topology, record)
            )
            record["trash_active"] = [
                entry["id"] for entry in self.retirement.enforce_trash()
            ]
            record["host_running"] = str(Path("/run/current-system").resolve())
            record["phase"] = (
                "complete"
                if record["host_running"] == manifest["host"]
                else "reboot-required"
            )
            record["verified_at"] = time.time()
            atomic_json(directory / "status.json", record)
            atomic_json(self.state / "latest.json", record)
            self.record_generation_verification(record)
            return record

    def wait_boot_guest(self, name, expected, timeout=180):
        """Bound readiness checks for both boot and deployment verification."""
        deadline = time.monotonic() + timeout
        last_error = "not running yet"
        while time.monotonic() < deadline:
            try:
                if name in self.running(timeout=max(0.1, deadline - time.monotonic())):
                    actual = self.command(
                        "qrexec-client",
                        "-d",
                        name,
                        "user:readlink -f /run/current-system",
                        timeout=max(0.1, min(10, deadline - time.monotonic())),
                    )
                    if actual != expected:
                        raise DeploymentError(
                            f"{name}: running system differs from release: {actual}"
                        )
                    return actual
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise DeploymentError(f"{name}: readiness timed out: {last_error}")

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
            trash = self.retirement.enforce_trash()
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
                if name not in self.running():
                    try:
                        self.command(
                            "qvm-start", "--skip-if-running", name, timeout=180
                        )
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        # Another Qubes caller may still be starting this VM.
                        # The bounded readiness check below decides the outcome.
                        pass
                    else:
                        started.append(name)
                self.wait_boot_guest(
                    name, topology["domains"][name]["backend"]["system"]["source"]
                )
            observed = self.verify(topology, set(starts) | set(require_running))
            if selected:
                manifest, _ = load_release(selected["release"])
                current_host = str(Path("/run/current-system").resolve())
                if current_host == manifest["host"]:
                    status.update(phase="complete", observed=observed,
                                  host_running=current_host, host_selected=manifest["host"])
                    self.record_generation_verification(status)
            return {
                "autostart": starts,
                "started": started,
                "observed": observed,
                "trash_active": [entry["id"] for entry in trash],
            }

    def boot_setup(self, default_setup, args, *, gui=False, background=False):
        with self.locked() as lock:
            if gui or background:
                if gui and background:
                    raise DeploymentError("choose either GUI or background setup")
                BootConfiguration(self).configure(default_setup, args, gui=gui)
                return
            # Keep the lock across exec through the exact setup wrapper.
            os.set_inheritable(lock.fileno(), True)
            self._boot_setup(default_setup, args)

    def _boot_setup(self, default_setup, args):
        selected = self.pointer()
        if not selected:
            staged = Path("/var/lib/airlock/host-store/boot-staged.json")
            if staged.exists():
                os.environ["AIRLOCK_STAGED_GENERATIONS"] = str(staged)
            else:
                os.environ.pop("AIRLOCK_STAGED_GENERATIONS", None)
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
