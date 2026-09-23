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
from .state import read_json, atomic_json, load_release
from .validation import validate_staged_generation, validate_admission_receipt
from .models import DeploymentPhase, DeploymentPlan, DeploymentRecord, Selection

STATE = Path("/var/lib/airlock/machine")


def run(*args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, **kwargs
    ).stdout.strip()


def active_profile():
    marker = Path("/etc/airlock/machine-profile")
    if marker.exists():
        return marker.read_text().strip()
    # Compatibility with images predating the deployment coordinator.
    for profile in ("usb-lab", "usb-gui-lab"):
        if Path("/etc/airlock", profile).exists():
            return profile
    raise DeploymentError("this boot profile does not support machine deployment")


class Coordinator:
    def __init__(self, state=STATE, command=run):
        self.state = Path(state)
        self.command = command
        self.profiles = Path("/nix/var/nix/profiles")
        self.retired_policy = RETIRED_POLICY
        self.retirement = RetirementManager(self)
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
        validate_staged_generation(
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

    def apply(self, release, recover=False) -> DeploymentRecord:
        release = Path(release).resolve()
        self.releases.require_available_release(release)
        manifest, topology = load_release(release)
        with self.locked():
            self.releases.require_no_pruning()
            self.releases.require_available_release(release)
            latest = self.state / "latest.json"
            if latest.exists() and not recover:
                existing = read_json(latest)
                if existing["phase"] in ("complete", "reboot-required") and existing[
                    "release"
                ] == str(release):
                    self.verify(topology, set(existing["plan"]["start"]))
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
            changes["stop"] = [name for name in changes["stop"] if name in runtime]
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
            transaction = str(time.time_ns())
            directory = self.state / "transactions" / transaction
            directory.mkdir(parents=True)
            record: DeploymentRecord = {
                "id": transaction,
                "release": str(release),
                "previous": str(baseline),
                "revision": manifest["revision"],
                "plan": changes,
                "phase": "preflight",
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
                    self.pin("host-" + transaction, manifest["host"])
                self.install_boot_hook(release, baseline)
                self.stage(release, topology, changes["stage"], directory)
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
                    self.command(
                        "qvm-shutdown", "--wait", "--timeout", "120", name, timeout=150
                    )
                phase("retiring")
                retired = [self.retirement.retire(entry) for entry in trash_plan]
                phase("configuring")
                self.setup(release, directory)
                restored = [
                    self.retirement.restore(entry, release, transaction)
                    for entry in restore_plan
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
                        if diagnostics:
                            diagnostics.event("vm-start", domain=name)
                            diagnostics.capture(
                                name + "-pci-before-start.txt",
                                ["qvm-device", "pci", "list", "--assignments", name],
                            )
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
            record["observed"] = self.verify(topology, set(record["plan"]["start"]))
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
