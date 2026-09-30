"""Reuse applied Qubes configuration; keep provisioning outside ordinary boot.

The receipt is an optimization of the existing converger, not a VM registry or
an integrity authority. Qubes still verifies and starts each VM's storage, and
the hardware and qrexec guards still run on every boot.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from .common import DeploymentError
from .planning import dependencies, ordered
from .state import atomic_json, load_release, read_json


SCHEMA = "airlock.applied-boot-configuration.v1"
VM_DEFAULTS = {
    "clockvm",
    "default_dispvm",
    "default_netvm",
    "default_guivm",
    "default_audiovm",
    "default_template",
    "management_dispvm",
    "updatevm",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def xml_value(node):
    """Ignore XML formatting and child ordering, not configuration values."""
    if node is None:
        raise DeploymentError("incomplete Qubes configuration; use recovery")
    return [
        node.tag,
        sorted(node.attrib.items()),
        (node.text or "").strip(),
        sorted((xml_value(child) for child in node), key=lambda v: json.dumps(v)),
    ]


class BootConfiguration:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.receipt = coordinator.state / "boot-configuration.json"
        self.system = Path(
            os.environ.get("AIRLOCK_SYSTEM_STATE_DIR", "/var/lib/airlock/system")
        )
        self.runtime = Path(
            os.environ.get("AIRLOCK_STARTUP_STATE_DIR", "/run/airlock/startup")
        )
        self.xml = Path(os.environ.get("AIRLOCK_QUBES_XML", "/var/lib/qubes/qubes.xml"))
        self.policy = Path(
            os.environ.get(
                "AIRLOCK_QUBES_POLICY_DIR", "/var/lib/airlock/qubes-policy.d"
            )
        ) / os.environ.get("AIRLOCK_QUBES_MANAGED_POLICY_FILE", "90-airlock.policy")

    def selection(self, default_setup, default_topology):
        # A failed transaction can precede selection; never bootstrap over it.
        latest = self.coordinator.state / "latest.json"
        if latest.exists() and read_json(latest)["phase"] not in (
            "complete",
            "reboot-required",
        ):
            raise DeploymentError(
                "interrupted deployment; resume or rollback in recovery"
            )
        selected = self.coordinator.pointer()
        if selected:
            from .coordinator import active_profile

            if selected["profile"] != active_profile():
                raise DeploymentError(
                    "selected deployment belongs to another boot profile; use recovery"
                )
            status = read_json(
                self.coordinator.state
                / "transactions"
                / selected["transaction"]
                / "status.json"
            )
            if status["phase"] not in ("complete", "reboot-required"):
                raise DeploymentError(
                    "interrupted deployment; resume or rollback in recovery"
                )
            release, topology = load_release(selected["release"])
            if release["profile"] != selected["profile"]:
                raise DeploymentError("selected release profile differs from selection")
            setup = release["setup"]
            staged = (
                Path(selected["release"]) / "staged.json"
                if release.get("stagedGenerations")
                else None
            )
            if staged is not None and (
                not staged.is_file()
                or read_json(staged) != release["stagedGenerations"]
            ):
                raise DeploymentError(
                    "selected release staged generation map is missing or inconsistent"
                )
        else:
            topology = read_json(default_topology)
            setup = default_setup
            staged = Path("/var/lib/airlock/host-store/boot-staged.json")
            if not staged.exists():
                staged = None
        if topology.get("schema") != "airlock.qubes-topology.v1":
            raise DeploymentError("unsupported startup topology schema")
        generations = read_json(staged) if staged is not None else {}
        identity = digest(
            {"topology": topology, "setup": setup, "generations": generations}
        )
        return setup, topology, staged, identity

    def gui_domains(self, topology):
        if "sys-gui" not in topology["domains"]:
            raise DeploymentError("physical GUI startup requires a declared sys-gui")
        required = {"sys-gui"}
        deps = dependencies(topology)
        default_netvm = topology.get("prefs", {}).get("default_netvm")
        for name, domain in topology["domains"].items():
            if (
                "netvm" not in domain
                and default_netvm in topology["domains"]
                and default_netvm != name
            ):
                deps[name].add(default_netvm)
        while True:
            expanded = required | set().union(*(deps[name] for name in required))
            if expanded == required:
                break
            required = expanded
        return [name for name in ordered(topology) if name in required]

    def filtered_topology(self, topology, names):
        result = deepcopy(topology)
        result["domains"] = {name: result["domains"][name] for name in names}
        result["prefs"] = {
            key: value
            for key, value in result.get("prefs", {}).items()
            if key not in VM_DEFAULTS
            or value not in topology["domains"]
            or value in names
        }
        kernels = {
            domain.get("backend", {}).get("kernel", {}).get("name")
            for domain in result["domains"].values()
        }
        kernels.update(
            domain.get("prefs", {}).get("kernel")
            for domain in result["domains"].values()
        )
        kernels.add(result["prefs"].get("default_kernel"))
        result["kernels"] = {
            name: value
            for name, value in result.get("kernels", {}).items()
            if name in kernels
        }
        templates = {domain.get("template") for domain in result["domains"].values()}
        result["templates"] = {
            name: value
            for name, value in result.get("templates", {}).items()
            if name in templates
        }
        return result

    def snapshot(self, topology, names):
        """One saved-state read replaces hundreds of per-property qvm queries.

        Immutable artifact metadata changes invalidate reuse and send the caller
        through the existing integrity validation. Writable guest data is only
        checked for presence/size, never hashed or used to invalidate setup.
        """
        try:
            root = ET.parse(self.xml).getroot()
        except ET.ParseError as exc:
            raise DeploymentError(
                "invalid saved Qubes configuration; use recovery"
            ) from exc
        pools = {p.get("name"): p.attrib for p in root.findall("pools/pool")}
        domains = {
            d.findtext("properties/property[@name='name']"): d
            for d in root.findall("domains/domain")
        }
        owners = {}
        for name, domain in domains.items():
            for volume in domain.findall("volume-config/volume"):
                if volume.get("name") not in ("root", "private"):
                    continue
                pool = volume.get("pool")
                if volume.get("source"):
                    # Managed standalone root/private volumes must not be
                    # snapshots of another domain's persistent data.
                    if (
                        name in names
                        and topology["domains"][name].get("backend", {}).get("type")
                        == "nix-closure"
                    ):
                        raise DeploymentError(
                            f"{name}: root/private volume has a foreign source"
                        )
                key = (pool, volume.get("vid"))
                if key in owners and (name in names or owners[key] in names):
                    raise DeploymentError(
                        f"{name}: persistent volume is shared with {owners[key]}"
                    )
                owners[key] = name
        # Also reject aliases through a different volume name or a snapshot's
        # source. Only the managed standalone disks require this isolation;
        # ordinary Qubes TemplateVM/AppVM sharing remains valid.
        protected = {
            key: owner
            for key, owner in owners.items()
            if owner in names
            and topology["domains"][owner].get("backend", {}).get("type")
            == "nix-closure"
        }
        for name, domain in domains.items():
            for volume in domain.findall("volume-config/volume"):
                for field in ("vid", "source"):
                    key = (volume.get("pool"), volume.get(field))
                    if key in protected and name != protected[key]:
                        raise DeploymentError(
                            f"{name}: volume references {protected[key]}'s persistent disk"
                        )
        shared = digest(
            {
                "pools": pools,
                "labels": xml_value(root.find("labels")),
                "prefs": xml_value(root.find("properties")),
                "owners": sorted(
                    [list(key) + [name] for key, name in owners.items()], key=json.dumps
                ),
                "policy": hashlib.sha256(self.policy.read_bytes()).hexdigest(),
            }
        )
        result = {}
        for name in names:
            if name not in domains:
                raise DeploymentError(
                    f"startup VM is missing: {name}; use recovery to repair configuration"
                )
            node = deepcopy(domains[name])
            if (
                topology["domains"][name].get("backend", {}).get("type")
                == "nix-closure"
            ):
                volumes = {v.get("name") for v in node.findall("volume-config/volume")}
                if not {"root", "private", "store", "store-data"} <= volumes:
                    raise DeploymentError(
                        f"{name}: persistent or closure volumes are missing; use recovery"
                    )
            if node.find("tags/tag[@name='airlock-retired']") is not None:
                raise DeploymentError(f"startup VM is retired: {name}")
            # Guest-advertised runtime features and automatic GUI/audio tags are
            # not configuration applied by setup-config.
            features = node.find("features")
            declared = topology["domains"][name].get("features", {})
            if features is not None:
                for feature in list(features):
                    key = feature.get("name", "")
                    if key not in declared and not key.startswith(
                        ("airlock-", "service.")
                    ):
                        features.remove(feature)
            tags = node.find("tags")
            if tags is not None:
                node.remove(tags)
            artifacts = {}
            disks = {}
            for volume in node.findall("volume-config/volume"):
                kind = volume.get("name")
                pool = pools.get(volume.get("pool"), {})
                if kind in ("root", "private") and pool.get("driver") == "file":
                    path = Path(pool["dir_path"]) / (volume.get("vid", "") + ".img")
                    if not path.is_file() or path.stat().st_size < int(
                        volume.get("size", "0")
                    ):
                        raise DeploymentError(
                            f"{name}: {kind} disk is missing or truncated; use recovery"
                        )
                    disks[str(path)] = path.stat().st_size
                if kind == "volatile":
                    node.find("volume-config").remove(volume)
                    continue
                for field in ("view_path", "blob_map_path", "manifest_path"):
                    if volume.get(field):
                        path = Path(volume.get(field))
                        info = path.stat()
                        artifacts[str(path)] = [
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                            info.st_mode,
                        ]
                        if field == "manifest_path":
                            manifest = read_json(path)
                            descriptor = manifest.get("root", {}).get("artifact")
                            if descriptor:
                                directory = (
                                    Path(pool["root_dir"])
                                    / "root-artifacts"
                                    / descriptor["sha256"]
                                )
                                for artifact in (
                                    directory / "root.img",
                                    directory / "artifact.json",
                                ):
                                    info = artifact.stat()
                                    artifacts[str(artifact)] = [
                                        info.st_dev,
                                        info.st_ino,
                                        info.st_size,
                                        info.st_mtime_ns,
                                        info.st_ctime_ns,
                                        info.st_mode,
                                    ]
                if kind == "kernel" and volume.get("vid"):
                    directory = Path(
                        pool.get("dir_path", "/var/lib/qubes/vm-kernels")
                    ) / volume.get("vid")
                    if not (directory / "vmlinuz").is_file():
                        raise DeploymentError(
                            f"{name}: VM kernel is missing; use recovery"
                        )
                    for path in sorted(directory.iterdir()):
                        if path.is_file():
                            info = path.stat()
                            artifacts[str(path)] = [
                                info.st_dev,
                                info.st_ino,
                                info.st_size,
                                info.st_mtime_ns,
                                info.st_ctime_ns,
                                info.st_mode,
                            ]
            result[name] = digest(
                {"xml": xml_value(node), "artifacts": artifacts, "disks": disks}
            )
        return shared, result

    def read_receipt(self):
        record = read_json(self.receipt)
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise DeploymentError(
                "unsupported applied boot configuration; use recovery"
            )

        def fingerprint(value):
            return (
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            )

        if (
            not fingerprint(record.get("identity"))
            or not fingerprint(record.get("shared"))
            or not isinstance(record.get("domains"), dict)
            or any(
                not isinstance(name, str) or not fingerprint(value)
                for name, value in record["domains"].items()
            )
        ):
            raise DeploymentError("invalid applied boot configuration; use recovery")
        return record

    def cached(self, identity, topology, names):
        if not self.receipt.exists():
            return False
        record = self.read_receipt()
        known = [name for name in names if name in record.get("domains", {})]
        if len(known) != len(names):
            if known:
                self.snapshot(topology, known)
            return False
        if record.get("identity") != identity:
            # An update may add VMs, but must not silently recreate a VM that
            # was previously applied and then disappeared with its private data.
            if known:
                self.snapshot(topology, known)
            return False
        # Missing state is actionable; don't recreate a lost private disk.
        shared, snapshots = self.snapshot(topology, names)
        return record.get("shared") == shared and all(
            record.get("domains", {}).get(name) == value
            for name, value in snapshots.items()
        )

    def record(self, identity, topology, names):
        shared, snapshots = self.snapshot(topology, names)
        previous = self.read_receipt() if self.receipt.exists() else {}
        if previous.get("identity") == identity and previous.get("shared") == shared:
            snapshots = {**previous.get("domains", {}), **snapshots}
        atomic_json(
            self.receipt,
            {
                "schema": SCHEMA,
                "identity": identity,
                "shared": shared,
                "domains": snapshots,
            },
        )

    def configure(self, default_setup, args, *, gui):
        if list(args) != ["--apply"]:
            raise DeploymentError("scoped boot setup accepts only --apply")
        started = time.monotonic()
        setup, topology, staged, identity = self.selection(
            default_setup, "/etc/airlock/qubes-topology.json"
        )
        gui_names = self.gui_domains(topology)
        names = (
            gui_names
            if gui
            else [name for name in ordered(topology) if name not in gui_names]
        )
        log_dir = Path(os.environ.get("AIRLOCK_LOG_DIR", "/var/log/airlock"))
        reused = self.cached(identity, topology, names)
        result = {}
        if gui:
            atomic_json(self.runtime / "topology.json", topology)
            device = os.environ.get("AIRLOCK_SETUP_CONFIG_STATUS_DEVICE")
            if reused and device:
                try:
                    Path(device).write_text(
                        "\033[H\033[J\n  Airlock | Startup\n\n  Starting your desktop…\n"
                    )
                except OSError:
                    pass  # Losing console output must not change boot behavior.
            for service in ("qubes-db-dom0", "qubesd", "qubes-qrexec-policy-daemon"):
                self.coordinator.command(
                    "systemctl",
                    "is-active",
                    "--quiet",
                    service + ".service",
                    timeout=15,
                )
        elif not self.cached(identity, topology, gui_names):
            raise DeploymentError(
                "GUI configuration changed after startup; repair in recovery"
            )
        if not reused:
            scoped = self.filtered_topology(topology, names)
            # The GPU checker always needs sys-gui's original descriptor, even
            # when the remaining VMs are configured after GPU handoff.
            env = dict(os.environ, AIRLOCK_SETUP_CONFIG_PERSIST_STATE="0")
            env.pop("AIRLOCK_STAGED_GENERATIONS", None)
            if staged is not None:
                env["AIRLOCK_STAGED_GENERATIONS"] = str(staged)
            if not gui:
                env.pop("AIRLOCK_SETUP_CONFIG_STATUS_DEVICE", None)
                env["AIRLOCK_GUI_GPU_CHECK_TOPOLOGY"] = str(
                    self.runtime / "topology.json"
                )
            path = self.runtime / (
                "gui-topology.json" if gui else "background-topology.json"
            )
            atomic_json(path, scoped)
            self.coordinator.command(setup, "--apply", "--topology", str(path), env=env)
            result = read_json(log_dir / "setup-config.json")
            if (
                result.get("schema") != "airlock.setup-config.v1"
                or result.get("status") != "passed"
                or result.get("restart_required")
                or result.get("unsupported_domains_count")
            ):
                raise DeploymentError(
                    "startup configuration left failed or deferred work; inspect setup-config.json"
                )
            self.record(identity, topology, names)
        if gui:
            atomic_json(self.runtime / "gui-setup-result.json", result)
            for name in gui_names:
                if name != "sys-gui":
                    self.coordinator.command(
                        "airlock-start-qube-ready",
                        name,
                        str(self.runtime / "topology.json"),
                        timeout=300,
                    )
        else:
            # Both scopes are now complete. Never persist a partial topology as
            # the image baseline used by deployment and rollback.
            self.record(identity, topology, list(topology["domains"]))
            atomic_json(self.system / "current-topology.json", topology)
        report = {
            "schema": "airlock.boot-configuration-result.v1",
            "status": "passed",
            "scope": "gui" if gui else "background",
            "reused": reused,
            "domains": names,
            "elapsed_seconds": time.monotonic() - started,
        }
        atomic_json(log_dir / "boot-configuration.json", report)
        if not gui:
            gui_result = read_json(self.runtime / "gui-setup-result.json")
            counts = {
                key: gui_result.get(key, 0) + result.get(key, 0)
                for key in (
                    "templates_changed",
                    "kernels_changed",
                    "domains_changed",
                    "prefs_changed",
                    "features_changed",
                    "services_changed",
                    "devices_changed",
                    "qrexec_policy_changed",
                    "root_images_changed",
                    "store_volumes_changed",
                    "state_volumes_changed",
                    "store_views_changed",
                    "qubesd_restarts",
                )
            }
            atomic_json(
                self.system / "current-setup.json",
                {
                    "schema": "airlock.setup-config.v1",
                    "status": "passed",
                    "mode": "apply",
                    "scope": "all",
                    "reason": "GUI and background configuration are ready",
                    "boot_configuration": report,
                    "unsupported_domains_count": 0,
                    "restart_required": False,
                    **counts,
                },
            )
        print(json.dumps(report), flush=True)
