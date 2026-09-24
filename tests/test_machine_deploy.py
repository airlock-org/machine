"""Whole-machine deployment contracts, including interrupted transactions."""

from copy import deepcopy
import importlib
import os
import subprocess
import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))
deploy = importlib.import_module("airlock_machine_deploy.coordinator")
planning = importlib.import_module("airlock_machine_deploy.planning")
releases = importlib.import_module("airlock_machine_deploy.releases")
common = importlib.import_module("airlock_machine_deploy.common")


def domain(name, netvm=None, guivm=None):
    return {
        "class": "StandaloneVM",
        "template": None,
        "netvm": netvm,
        "devices": {},
        "prefs": {"autostart": True, "guivm": guivm},
        "features": {},
        "services": {},
        "backend": {
            "type": "nix-closure",
            "state": {"persistent": name + "-private"},
            "system": {"source": "/nix/store/" + name + "-system"},
            "root": {"source": "/nix/store/root/root.img"},
            "kernel": {"name": "qubes-kernel"},
            "store": {
                "source": "/nix/store/" + name + "-closure",
                "mode": "shared-closure",
                "device": "/dev/xvde",
                "blobDevice": "/dev/xvdf",
                "mountpoint": "/nix/store",
                "viewBackend": "erofs-objectmap",
            },
        },
    }


@pytest.fixture
def topology():
    return {
        "schema": "airlock.qubes-topology.v1",
        "kernels": {},
        "prefs": {},
        "domains": {
            "sys-gui": domain("sys-gui"),
            "sys-dev": domain("sys-dev", guivm="sys-gui"),
            "usr-browser": domain("usr-browser", netvm="sys-dev", guivm="sys-gui"),
            "usr-home": domain("usr-home", guivm="sys-gui"),
        },
    }


def test_dependency_order_and_affected_closure(topology):
    new = deepcopy(topology)
    new["domains"]["sys-gui"]["backend"]["system"]["source"] += "-new"
    result = planning.plan(topology, new, set(topology["domains"]))
    assert result["changed"] == ["sys-gui"]
    assert result["stop"][-1] == "sys-gui"
    assert result["start"][0] == "sys-gui"
    assert result["stop"].index("usr-browser") < result["stop"].index("sys-dev")
    assert set(result["affected"]) == set(topology["domains"])


def test_app_change_does_not_restart_providers(topology):
    new = deepcopy(topology)
    new["domains"]["usr-home"]["prefs"]["vcpus"] = 2
    result = planning.plan(topology, new, set(topology["domains"]))
    assert result["stop"] == ["usr-home"]
    assert result["stage"] == []


def test_network_pci_handoff_stops_old_provider_and_orders_new_chain(topology):
    old_dev = topology["domains"]["sys-dev"]
    old_dev["features"] = {"airlock-auto-network-pci": "1"}
    old_dev["prefs"]["provides_network"] = True
    new = deepcopy(topology)
    new_dev = new["domains"]["sys-dev"]
    new_dev["features"]["airlock-auto-network-pci"] = "0"
    new_dev["prefs"]["provides_network"] = False
    new_dev["netvm"] = "sys-firewall"
    new["domains"]["sys-net"] = domain("sys-net")
    new["domains"]["sys-net"]["prefs"]["provides_network"] = True
    new["domains"]["sys-net"]["features"] = {"airlock-auto-network-pci": "1"}
    new["domains"]["sys-firewall"] = domain("sys-firewall", netvm="sys-net")
    new["domains"]["sys-firewall"]["prefs"]["provides_network"] = True
    new["domains"]["usr-browser"]["netvm"] = "sys-firewall"

    migration = planning.plan(topology, new, {"sys-dev", "usr-browser"})
    assert migration["pci_handoff"] == {"from": "sys-dev", "to": "sys-net"}
    assert migration["stop"].index("usr-browser") < migration["stop"].index("sys-dev")
    assert migration["start"].index("sys-net") < migration["start"].index("sys-firewall")
    assert migration["start"].index("sys-firewall") < migration["start"].index("sys-dev")

    rollback = planning.plan(
        new, topology, {"sys-dev", "sys-net", "sys-firewall"}, retain_removed=True
    )
    assert rollback["pci_handoff"] == {"from": "sys-net", "to": "sys-dev"}
    assert rollback["stop"].index("sys-dev") < rollback["stop"].index("sys-firewall")
    assert rollback["stop"].index("sys-firewall") < rollback["stop"].index("sys-net")


def test_network_pci_handoff_releases_only_old_owners_network_nics(monkeypatch, tmp_path):
    class FakeInterface:
        def __init__(self, value):
            self.value = value

        def __repr__(self):
            return self.value

    class FakeDevice:
        def __init__(self, port_id, interface):
            self.port_id = port_id
            self.interfaces = [FakeInterface(interface)]

    network = FakeDevice("00_02.0", "p0200")
    unrelated = FakeDevice("00_08.0", "p0300")
    app = SimpleNamespace(domains={"dom0": SimpleNamespace(devices={"pci": [network, unrelated]})})
    monkeypatch.setitem(sys.modules, "qubesadmin", SimpleNamespace(Qubes=lambda: app))
    commands = []

    def command(*args):
        commands.append(args)
        if args[:3] == ("qvm-device", "pci", "list"):
            return "dom0:00_02.0  *sys-dev (required)\ndom0:00_08.0  *sys-dev (required)"
        return ""

    coordinator = deploy.Coordinator(state=tmp_path, command=command)
    coordinator.handoff_network_pci({"from": "sys-dev", "to": "sys-net"})
    assert commands == [
        ("qvm-device", "pci", "list", "--assignments", "dom0"),
        ("qvm-device", "pci", "unassign", "sys-dev", "dom0:00_02.0"),
    ]


def test_cycle_fails(topology):
    topology["domains"]["sys-gui"]["netvm"] = "sys-dev"
    with pytest.raises(deploy.DeploymentError, match="cycle"):
        planning.ordered(topology)


@pytest.mark.parametrize(
    "change", ["remove", "class", "devices", "state", "features", "backend"]
)
def test_unsupported_changes_fail(topology, change):
    new = deepcopy(topology)
    vm = new["domains"]["usr-home"]
    if change == "remove":
        del new["domains"]["usr-home"]
    elif change == "state":
        vm["backend"]["state"] = {"persistent": "different"}
    elif change == "features":
        topology["domains"]["usr-home"]["features"]["test"] = "1"
    elif change == "backend":
        vm["backend"]["type"] = "qubes-template"
    else:
        vm[change] = "different"
    with pytest.raises(deploy.DeploymentError):
        planning.plan(topology, new, set())


def test_removal_requires_exact_authorization_and_no_surviving_dependencies(topology):
    new = deepcopy(topology)
    del new["domains"]["usr-home"]
    result = planning.plan(topology, new, set(), retire_removed=["usr-home"])
    assert result["retire"] == ["usr-home"]
    with pytest.raises(deploy.DeploymentError, match="does not match"):
        planning.plan(topology, new, set(), retire_removed=["usr-browser"])

    dependent = deepcopy(topology)
    del dependent["domains"]["sys-dev"]
    with pytest.raises(deploy.DeploymentError, match="still depends"):
        planning.plan(topology, dependent, set(), retire_removed=["sys-dev"])


@pytest.mark.parametrize(
    "field",
    [
        "clockvm",
        "default_dispvm",
        "default_netvm",
        "default_guivm",
        "default_audiovm",
        "default_template",
        "management_dispvm",
        "updatevm",
    ],
)
def test_retirement_rejects_machine_default_reference(topology, field):
    new = deepcopy(topology)
    del new["domains"]["usr-home"]
    new["prefs"][field] = "usr-home"
    with pytest.raises(deploy.DeploymentError, match="still depends|still references"):
        planning.plan(topology, new, set(), retire_removed=["usr-home"])


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_retirement_rejects_policy_reference(topology, endpoint):
    new = deepcopy(topology)
    del new["domains"]["usr-home"]
    new["qrexec_policy"] = {"rules": [{"source": "sys-dev", "target": "sys-gui"}]}
    new["qrexec_policy"]["rules"][0][endpoint] = "usr-home"
    with pytest.raises(deploy.DeploymentError, match=f"rules\\[0\\].{endpoint}"):
        planning.plan(topology, new, set(), retire_removed=["usr-home"])


def test_retirement_rejects_domain_dispvm_reference(topology):
    new = deepcopy(topology)
    del new["domains"]["usr-home"]
    new["domains"]["sys-dev"]["prefs"]["default_dispvm"] = "usr-home"
    with pytest.raises(deploy.DeploymentError, match="still depends"):
        planning.plan(topology, new, set(), retire_removed=["usr-home"])


@pytest.mark.parametrize("reference", ["netvm", "default_netvm", "qrexec_target"])
def test_later_release_cannot_reference_already_trashed_vm(topology, reference):
    retired = deepcopy(topology)
    del retired["domains"]["usr-home"]
    later = deepcopy(retired)
    if reference == "netvm":
        later["domains"]["sys-dev"]["netvm"] = "usr-home"
    elif reference == "default_netvm":
        later["prefs"]["default_netvm"] = "usr-home"
    else:
        later["qrexec_policy"] = {
            "rules": [{"source": "sys-dev", "target": "usr-home"}]
        }
    with pytest.raises(deploy.DeploymentError, match="retired VM"):
        planning.plan(retired, later, set(), trashed={"usr-home": {}})


def test_restored_vm_can_be_referenced_after_identity_validation(topology):
    retired = deepcopy(topology)
    del retired["domains"]["usr-home"]
    restored = deepcopy(topology)
    restored["domains"]["sys-dev"]["netvm"] = "usr-home"
    result = planning.plan(retired, restored, set(), trashed={"usr-home": {}})
    assert result["restore"] == ["usr-home"]


def test_later_apply_rejects_reference_to_trash_before_disruption(
    machine, tmp_path, topology
):
    machine.apply(retirement_release(tmp_path, topology))
    later = deepcopy(topology)
    del later["domains"]["usr-home"]
    later["domains"]["sys-dev"]["netvm"] = "usr-home"
    release = write_release(tmp_path / "later", later)
    machine.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="retired VM"):
        machine.apply(release)
    assert not any(call[0] == "qvm-shutdown" for call in machine.calls)
    assert machine.retirement.active_trash()["usr-home"]["status"] == "trashed"


def test_boot_rejects_selected_topology_referencing_trash(machine, tmp_path, topology):
    release = retirement_release(tmp_path, topology)
    machine.apply(release)
    inconsistent = deploy.read_json(release / "topology.json")
    inconsistent["domains"]["sys-dev"]["netvm"] = "usr-home"
    deploy.atomic_json(release / "topology.json", inconsistent)
    with pytest.raises(deploy.DeploymentError, match="retired VM"):
        machine.boot_start("/missing-image")


def test_dependencies_started_even_without_autostart(topology):
    for vm in topology["domains"].values():
        vm["prefs"]["autostart"] = False
    result = planning.plan(topology, topology, {"usr-browser"})
    assert result["stop"] == []
    assert result["start"] == ["sys-gui", "sys-dev", "usr-browser"]


def write_release(path, topology, revision="new", retire=()):
    path.mkdir()
    deploy.atomic_json(
        path / "release.json",
        {
            "schema": deploy.SCHEMA,
            "profile": "usb-lab",
            "host": "/nix/store/host-" + revision,
            "revision": revision,
            "setup": "/nix/store/setup/bin/airlock-setup-config",
            "retireDomains": list(retire),
            "trashRetentionDays": 30,
        },
    )
    deploy.atomic_json(path / "topology.json", topology)
    return path


class Machine(deploy.Coordinator):
    def __init__(self, state, baseline, topology):
        self.calls = []
        self.live = set(topology["domains"])
        self.selected = {
            name: vm["backend"]["system"]["source"]
            for name, vm in topology["domains"].items()
        }
        self.initial = baseline
        self.fail = None
        self.fail_after = None
        self.pool_match = None
        self.generation_details = None
        self.generations = {name: 1 for name in topology["domains"]}
        self.tags = {name: set() for name in topology["domains"]}
        self.uuids = {name: "uuid-" + name for name in topology["domains"]}
        self.volumes = {
            name: {
                "private": {
                    "pool": "private-pool",
                    "vid": name + "/private",
                    "source": "",
                    "rw": True,
                    "save_on_stop": True,
                    "snap_on_start": False,
                }
            }
            for name in topology["domains"]
        }
        super().__init__(state, self.execute)
        self.profiles = state / "test-profiles"
        self.hosts = set()

    def volume_inventory(self):
        return deepcopy(self.volumes)

    def running_hosts(self):
        return self.hosts

    def execute(self, *args, **kwargs):
        self.calls.append(args)
        if self.fail and args[0] == self.fail:
            raise deploy.DeploymentError("simulated failure")
        if args[0] == "nix-store" and "--add-root" in args:
            root = Path(args[args.index("--add-root") + 1])
            root.unlink(missing_ok=True)
            root.symlink_to(args[-1])
        if args[0] == "nix-env" and "--delete-generations" in args:
            for number in args[args.index("--delete-generations") + 1 :]:
                (self.profiles / f"system-{number}-link").unlink()
        if args[0] == "qvm-ls":
            if "--raw-list" in args:
                return "\n".join(sorted(self.selected))
            return "\n".join(name + "|Running" for name in sorted(self.live))
        if args[0] == "airlock-closure-store":
            if args[1] in ("admit", "describe-generation", "find-generation"):
                assert not any(call[0] == "qvm-shutdown" for call in self.calls)
            if args[1] == "describe-generation":
                name = args[args.index("--domain") + 1]
                generation = int(args[args.index("--generation") + 1])
                if self.generation_details is not None:
                    return json.dumps(self.generation_details)
                return json.dumps(
                    {
                        "schema": deploy.GENERATION_INFO_SCHEMA,
                        "status": "passed",
                        "domain": name,
                        "generation": generation,
                        "system": self.selected[name],
                        "manifest": f"/pool/{name}/{generation}/manifest.json",
                        "retention": {"retain": True},
                    }
                )
            if args[1] == "find-generation":
                return json.dumps(
                    {
                        "status": "found" if self.pool_match is not None else "missing",
                        "generation": self.pool_match,
                    }
                )
            return json.dumps({"generation": "staged-generation"})
        if args[0] == "qvm-shutdown":
            self.live.remove(args[-1])
        if args[0] == "qvm-start":
            self.live.add(args[-1])
        if args[0] == "qvm-prefs":
            if args[2] == "uuid":
                return self.uuids[args[1]]
            return ""
        if args[0] == "qvm-tags":
            name, action = args[1:3]
            if action == "list":
                return "\n".join(sorted(self.tags[name]))
            if action == "add":
                self.tags[name].add(args[3])
            elif action == "del":
                self.tags[name].discard(args[3])
            return ""
        if args[0] == "qvm-remove":
            name = args[-1]
            self.live.discard(name)
            self.selected.pop(name, None)
            self.tags.pop(name, None)
            self.volumes.pop(name, None)
            return ""
        if args[0].endswith("/airlock-setup-config"):
            target = deploy.read_json(args[-1])
            for name in target["domains"]:
                self.generations.setdefault(name, 1)
                self.tags.setdefault(name, set())
                self.uuids.setdefault(name, "uuid-" + name)
                self.volumes.setdefault(
                    name,
                    {
                        "private": {
                            "pool": "private-pool",
                            "vid": name + "/private",
                            "source": "",
                            "rw": True,
                            "save_on_stop": True,
                            "snap_on_start": False,
                        }
                    },
                )
            for vm in target["domains"].values():
                for token in vm.get("prefs", {}).get("kernelopts", "").split():
                    if token.startswith("airlock.system="):
                        assert (
                            token
                            == "airlock.system=" + vm["backend"]["system"]["source"]
                        )
            self.selected.update(
                {
                    name: vm["backend"]["system"]["source"]
                    for name, vm in target["domains"].items()
                }
            )
            if self.fail_after == args[0]:
                raise deploy.DeploymentError(
                    "simulated failure after storage selection"
                )
            deploy.atomic_json(
                Path(kwargs["env"]["AIRLOCK_LOG_DIR"]) / "setup-config.json",
                {"restart_required": False},
            )
        if args[0] == "qvm-features":
            if args[2] == "airlock-store-generation":
                return str(self.generations[args[1]])
            return self.selected[args[1]]
        if args[0] == "qrexec-client":
            return self.selected[args[2]]
        return ""

    def baseline(self):
        selection = self.pointer()
        return Path(selection["release"]) if selection else self.initial

    def install_boot_hook(self, release, baseline):
        self.calls.append(("boot-hook",))


@pytest.fixture
def machine(tmp_path, topology, monkeypatch):
    monkeypatch.setattr(deploy, "active_profile", lambda: "usb-lab")
    policy = tmp_path / "05-airlock-retired.policy"
    policy.write_text("* * @anyvm @tag:airlock-retired deny\n")
    monkeypatch.setattr(deploy, "RETIRED_POLICY", str(policy))
    baseline = write_release(tmp_path / "baseline", topology, "old")
    return Machine(tmp_path / "state", baseline, topology)


def new_release(tmp_path, topology):
    new = deepcopy(topology)
    new["domains"]["usr-home"]["backend"]["system"]["source"] += "-new"
    return write_release(tmp_path / "new", new)


def guest_receipt(machine, topology, generation=17):
    revision = "a" * 40
    lock_hash = "b" * 64
    system = topology["domains"]["usr-home"]["backend"]["system"]["source"] + "-new"
    source = "/home/user/.local/share/airlock/machine-build/closures/usr-home"
    manifest = f"/var/lib/airlock/closure-store/domains/usr-home/generations/{generation}/manifest.json"
    store = topology["domains"]["usr-home"]["backend"]["store"]
    machine.generation_details = {
        "schema": deploy.GENERATION_INFO_SCHEMA,
        "status": "passed",
        "domain": "usr-home",
        "generation": generation,
        "current_generation": 1,
        "selected": False,
        "manifest": manifest,
        "system": system,
        "root_source": topology["domains"]["usr-home"]["backend"]["root"]["source"],
        "kernel_name": topology["domains"]["usr-home"]["backend"]["kernel"]["name"],
        "closure_source": source,
        "store": {
            "mode": "shared-closure",
            "mountpoint": store["mountpoint"],
            "device": store["device"],
            "blob_device": store["blobDevice"],
            "view_backend": store["viewBackend"],
            "closure_hash": "c" * 64,
        },
        "approval": {
            "approved_by": "sys-dev",
            "source": source,
            "transport": "qrexec",
        },
        "provenance": {"source_revision": revision, "lock_hash": lock_hash},
        "verification": {"status": "passed"},
        "retention": {"retain": True},
    }
    return {
        "schema": common.ADMISSION_SCHEMA,
        "status": "passed",
        "transport": "qrexec",
        "domain": "usr-home",
        "generation": generation,
        "previous_generation": 1,
        "current_generation": 1,
        "activated": False,
        "retained": True,
        "manifest": manifest,
        "system": system,
        "root_source": machine.generation_details["root_source"],
        "kernel_name": machine.generation_details["kernel_name"],
        "closure_hash": machine.generation_details["store"]["closure_hash"],
        "source_revision": revision,
        "lock_hash": lock_hash,
    }


def test_boot_start_image_autostart_and_dependencies(machine, tmp_path, topology):
    for vm in topology["domains"].values():
        vm["prefs"]["autostart"] = False
    topology["domains"]["usr-home"]["prefs"]["autostart"] = True
    path = tmp_path / "image.json"
    deploy.atomic_json(path, topology)
    machine.live = {"sys-gui", "sys-dev"}
    result = machine.boot_start(path, ["sys-gui"])
    assert result["started"] == ["usr-home"]
    assert machine.boot_start(path, ["sys-gui"])["started"] == []
    assert "usr-browser" not in machine.live


def test_boot_start_uses_selected_release_not_image(machine, tmp_path, topology):
    topology["domains"]["usr-browser"]["prefs"]["autostart"] = False
    machine.apply(new_release(tmp_path, topology))
    machine.live = {"sys-gui", "sys-dev"}
    assert machine.boot_start("/missing-image", ["sys-gui"])["started"] == ["usr-home"]


def test_boot_start_refuses_missing_gpu_guard(machine):
    machine.live = set()
    with pytest.raises(deploy.DeploymentError, match="guarded VMs"):
        machine.boot_start(machine.initial / "topology.json", ["sys-gui"])
    assert not any(call[0] == "qvm-start" for call in machine.calls)


@pytest.mark.parametrize("reason", ["profile", "interrupted"])
def test_boot_start_fails_closed(machine, tmp_path, topology, monkeypatch, reason):
    machine.apply(new_release(tmp_path, topology))
    machine.calls.clear()
    if reason == "profile":
        monkeypatch.setattr(deploy, "active_profile", lambda: "usb-gui-lab")
    else:
        selected = machine.pointer()
        deploy.atomic_json(
            machine.state / "transactions" / selected["transaction"] / "status.json",
            {"phase": "configuring"},
        )
    with pytest.raises(deploy.DeploymentError):
        machine.boot_start("/missing-image")
    assert not any(call[0] == "qvm-start" for call in machine.calls)


def test_guest_success_and_pending_host_are_distinct(machine, tmp_path, topology):
    release = new_release(tmp_path, topology)
    result = machine.apply(release)
    assert result["phase"] == "reboot-required"
    assert result["observed"]["usr-home"]["running"].endswith("-new")
    assert machine.pointer()["release"] == str(release)
    assert deploy.read_json(machine.state / "latest.json") == result
    assert not any("kill" in call[0] for call in machine.calls)


def test_stage_failure_never_stops_or_selects(machine, tmp_path, topology):
    machine.fail = "airlock-closure-store"
    with pytest.raises(deploy.DeploymentError):
        machine.apply(new_release(tmp_path, topology))
    assert machine.pointer() is None
    assert machine.status()["failed_phase"] == "staging"
    assert not any(call[0] == "qvm-shutdown" for call in machine.calls)


def test_stage_reuses_sys_dev_admitted_generation(machine, tmp_path, topology):
    machine.pool_match = 17
    result = machine.apply(new_release(tmp_path, topology))
    staged = deploy.read_json(
        machine.state / "transactions" / result["id"] / "staged.json"
    )
    assert staged == {"usr-home": 17}
    pool_calls = [call for call in machine.calls if call[0] == "airlock-closure-store"]
    assert len(pool_calls) == 1
    assert pool_calls[0][1] == "find-generation"


def test_guest_candidate_activates_exact_admitted_generation_and_rolls_back(
    machine, topology
):
    receipt = guest_receipt(machine, topology)
    preview = machine.inspect_guest(receipt)
    candidate = Path(preview["candidate"])
    assert preview["plan"]["changed"] == ["usr-home"]
    assert preview["plan"]["host_reboot_required"] is True
    assert deploy.read_json(candidate / "staged.json") == {"usr-home": 17}
    result = machine.apply(candidate)
    assert result["plan"]["changed"] == ["usr-home"]
    assert result["observed"]["usr-home"]["running"] == receipt["system"]
    staged = deploy.read_json(
        machine.state / "transactions" / result["id"] / "staged.json"
    )
    assert staged == {"usr-home": 17}
    assert not any(
        call[:2] == ("airlock-closure-store", "find-generation")
        for call in machine.calls
    )
    machine.calls.clear()
    rollback = machine.apply(machine.initial, recover=True)
    assert rollback["observed"]["usr-home"]["running"].endswith("-system")


def test_guest_candidate_updates_boot_system_and_preserves_other_options(
    machine, topology
):
    old = topology["domains"]["usr-home"]["backend"]["system"]["source"]
    opts = f"console=hvc0  airlock.system={old} init=/sbin/init airlock.store=/dev/xvde"
    topology["domains"]["usr-home"]["prefs"]["kernelopts"] = opts
    deploy.atomic_json(machine.initial / "topology.json", topology)
    receipt = guest_receipt(machine, topology)
    candidate = machine.guest_candidate(receipt)
    changed = deploy.read_json(candidate / "topology.json")
    assert changed["domains"]["usr-home"]["prefs"]["kernelopts"] == opts.replace(
        old, receipt["system"]
    )
    assert changed["domains"]["sys-dev"] == topology["domains"]["sys-dev"]
    assert machine.guest_candidate(receipt) == candidate
    machine.apply(candidate)
    machine.calls.clear()
    machine.apply(machine.initial, recover=True)


@pytest.mark.parametrize(
    "opts", ["airlock.system=/wrong", "airlock.system=/wrong airlock.system=/wrong"]
)
def test_guest_candidate_rejects_inconsistent_baseline_boot_options(
    machine, topology, opts
):
    topology["domains"]["usr-home"]["prefs"]["kernelopts"] = opts
    deploy.atomic_json(machine.initial / "topology.json", topology)
    with pytest.raises(deploy.DeploymentError, match="baseline guest boot system"):
        machine.guest_candidate(guest_receipt(machine, topology))


def test_guest_candidate_identity_includes_controller_version(
    machine, topology, monkeypatch
):
    receipt = guest_receipt(machine, topology)
    original = machine.guest_candidate(receipt)
    controller = "/nix/store/new-coordinator/bin/airlock-machine-deploy"
    monkeypatch.setenv("AIRLOCK_MACHINE_COORDINATOR", controller)
    updated = machine.guest_candidate(receipt)
    assert updated != original
    assert deploy.read_json(updated / "release.json")["coordinator"] == controller


@pytest.mark.parametrize("recovery", ["resume", "rollback"])
def test_interrupted_guest_activation_remains_recoverable(machine, topology, recovery):
    candidate = machine.guest_candidate(guest_receipt(machine, topology))
    machine.fail_after = "/nix/store/setup/bin/airlock-setup-config"
    with pytest.raises(deploy.DeploymentError, match="after storage selection"):
        machine.apply(candidate)
    assert machine.status()["failed_phase"] == "configuring"
    assert machine.pointer()["release"] == str(candidate)
    machine.fail_after = None
    machine.calls.clear()
    target = candidate if recovery == "resume" else machine.initial
    result = machine.apply(target, recover=True)
    assert result["phase"] == "reboot-required"
    expected = (
        machine.generation_details["system"]
        if recovery == "resume"
        else topology["domains"]["usr-home"]["backend"]["system"]["source"]
    )
    assert result["observed"]["usr-home"]["running"] == expected


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("selected", True, "already active"),
        ("root_source", "/nix/store/wrong-root", "receipt does not identify"),
        ("retention", {"retain": False}, "not retained"),
        (
            "provenance",
            {"source_revision": "f" * 40, "lock_hash": "b" * 64},
            "provenance",
        ),
    ],
)
def test_guest_candidate_rejects_untrusted_generation(
    machine, topology, field, value, error
):
    receipt = guest_receipt(machine, topology)
    machine.generation_details[field] = value
    with pytest.raises(deploy.DeploymentError, match=error):
        machine.guest_candidate(receipt)


def test_selected_guest_candidate_restores_staged_map_at_boot(
    machine, topology, monkeypatch
):
    candidate = machine.guest_candidate(guest_receipt(machine, topology))
    machine.apply(candidate)
    calls = []
    monkeypatch.delenv("AIRLOCK_STAGED_GENERATIONS", raising=False)
    monkeypatch.setattr(deploy.os, "execv", lambda *args: calls.append(args))
    machine.boot_setup("/image-setup", ["--apply"])
    assert calls
    assert deploy.os.environ["AIRLOCK_STAGED_GENERATIONS"] == str(
        candidate / "staged.json"
    )


def test_failure_requires_recovery_and_resume_preserves_previous(
    machine, tmp_path, topology
):
    machine.fail = "/nix/store/setup/bin/airlock-setup-config"
    release = new_release(tmp_path, topology)
    with pytest.raises(deploy.DeploymentError):
        machine.apply(release)
    assert machine.pointer()["release"] == str(release)
    with pytest.raises(deploy.DeploymentError, match="unfinished"):
        machine.apply(release)
    machine.fail = None
    machine.calls.clear()
    assert machine.apply(release, recover=True)["phase"] == "reboot-required"
    assert "usr-home" in machine.live
    assert machine.status()["previous"] == str(machine.initial)


def test_rollback_reapplies_whole_previous_release(machine, tmp_path, topology):
    machine.apply(new_release(tmp_path, topology))
    machine.calls.clear()
    result = machine.apply(machine.initial, recover=True)
    assert result["observed"]["usr-home"]["running"].endswith("-system")
    assert result["host_selected"] == "/nix/store/host-old"


def test_lock_prevents_two_writers(machine):
    with machine.locked():
        with pytest.raises(deploy.DeploymentError, match="another"):
            with machine.locked():
                pass


@pytest.mark.parametrize("operation", ["apply", "purge", "prune"])
def test_deployment_and_lifecycle_managers_share_the_lock(machine, operation):
    actions = {
        "apply": lambda: machine.apply(machine.initial),
        "purge": lambda: machine.retirement.purge_trash("1-usr-home", "1-usr-home"),
        "prune": lambda: machine.releases.prune_release(
            machine.initial, str(machine.initial)
        ),
    }
    machine.calls.clear()
    with machine.locked():
        with pytest.raises(deploy.DeploymentError, match="another"):
            actions[operation]()
    assert machine.calls == []


@pytest.mark.nix
def test_packaged_coordinator_imports_modules_without_checkout_or_pythonpath(tmp_path):
    wrapper = ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"
    installable = str(ROOT) + "#airlock-machine-deploy"
    build = subprocess.run(
        [str(wrapper), "build", "--no-link", "--print-out-paths", installable],
        check=True,
        capture_output=True,
        text=True,
    )
    package = Path(build.stdout.strip())
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME")
    }
    source = subprocess.run(
        [sys.executable, str(ROOT / "lifecycle/runtime/tools/machine-deploy.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    installed = subprocess.run(
        [
            str(wrapper), "shell", installable, "-c", "/bin/sh", "-c",
            'unset PYTHONPATH PYTHONHOME; export PATH=/nonexistent; cd /; exec "$@"',
            "sh", str(package / "bin/airlock-machine-deploy"), "--help",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # The packaged and development interpreters may wrap argparse help differently.
    assert installed.stdout.split() == source.stdout.split()
    assert "release-prune" in installed.stdout
    assert "trash-purge" in installed.stdout


@pytest.mark.parametrize("deferred", [False, True])
def test_native_boot_hooks_do_not_write_readonly_systemd_tree(
    tmp_path, topology, monkeypatch, deferred
):
    release = write_release(tmp_path / "release", topology)
    manifest = deploy.read_json(release / "release.json")
    manifest["coordinator"] = "/nix/store/new-engine/bin/airlock-machine-deploy"
    deploy.atomic_json(release / "release.json", manifest)
    state = tmp_path / "state"
    state.mkdir()
    calls = []

    def command(*args):
        calls.append(args)
        if args[0] == "nix-store":
            assert args == (
                "nix-store",
                "--add-root",
                str(state / "roots" / "new-engine"),
                "--realise",
                "/nix/store/new-engine",
            )
            return ""
        assert args[:2] == ("systemctl", "show")
        if args[3] == "--property=LoadState":
            return (
                "not-found"
                if args[2].endswith("-deferred") and not deferred
                else "loaded"
            )
        assert args[3] == "--property=ExecStart"
        return (
            "{ path=/nix/store/old-engine/bin/airlock-machine-deploy ; "
            "argv[]=/nix/store/old-engine/bin/airlock-machine-deploy boot-setup "
            "/nix/store/setup/bin/airlock-setup-config --apply ; }"
        )

    def guarded_path(*args):
        assert args[0] != "/etc/systemd/system", "must not mutate declarative units"
        return Path(*args)

    monkeypatch.setattr(deploy, "Path", guarded_path)
    deploy.Coordinator(state, command).install_boot_hook(release, release)
    assert str((state / "controller").readlink()) == manifest["coordinator"]
    assert not any(call[1] == "daemon-reload" for call in calls)


def test_profile_mismatch_fails_before_staging(
    machine, tmp_path, topology, monkeypatch
):
    monkeypatch.setattr(deploy, "active_profile", lambda: "usb-gui-lab")
    with pytest.raises(deploy.DeploymentError, match="profile"):
        machine.apply(new_release(tmp_path, topology))
    assert not machine.calls


def test_boot_refuses_interrupted_selection(machine, tmp_path, topology):
    machine.fail = "/nix/store/setup/bin/airlock-setup-config"
    with pytest.raises(deploy.DeploymentError):
        machine.apply(new_release(tmp_path, topology))
    with pytest.raises(deploy.DeploymentError, match="interrupted"):
        machine.boot_setup("/fallback", ["--apply"])


def test_boot_uses_selected_topology_not_image_seed(
    machine, tmp_path, topology, monkeypatch
):
    release = new_release(tmp_path, topology)
    machine.apply(release)
    calls = []
    monkeypatch.setattr(deploy.os, "execv", lambda *args: calls.append(args))
    machine.boot_setup("/image-setup", ["--apply"])
    assert calls[0][1][-2:] == ["--topology", str(release / "topology.json")]


def test_wrong_guest_generation_is_not_success(machine, topology):
    machine.selected["usr-home"] = "/nix/store/wrong-system"
    with pytest.raises(deploy.DeploymentError, match="selected system"):
        machine.verify(topology, machine.live)


def test_rollback_after_vm_addition_keeps_new_vm_halted(machine, tmp_path, topology):
    new = deepcopy(topology)
    new["domains"]["usr-extra"] = domain("usr-extra")
    machine.apply(write_release(tmp_path / "extra", new))
    assert "usr-extra" in machine.live
    machine.calls.clear()
    result = machine.apply(machine.initial, recover=True)
    assert "usr-extra" not in machine.live
    assert result["plan"]["retire"] == ["usr-extra"]
    assert result["plan"]["retained_undeclared"] == ["usr-extra"]
    assert machine.retirement.active_trash()["usr-extra"]["status"] == "trashed"
    assert machine.tags["usr-extra"] == {"airlock-retired"}
    assert all(call[0] != "qvm-remove" for call in machine.calls)


def retirement_release(tmp_path, topology, name="usr-home"):
    retired = deepcopy(topology)
    del retired["domains"][name]
    return write_release(tmp_path / "retired", retired, "retired", retire=[name])


def test_retirement_policy_preflight_does_not_invoke_shell_builtin(
    machine, tmp_path, topology
):
    machine.inspect(retirement_release(tmp_path, topology))
    assert not any(call[0] == "test" for call in machine.calls)


def test_retirement_rejects_missing_policy(machine, tmp_path, topology):
    Path(deploy.RETIRED_POLICY).unlink()
    with pytest.raises(deploy.DeploymentError, match="policy is not installed"):
        machine.inspect(retirement_release(tmp_path, topology))


def test_retirement_is_recoverable_and_rollback_restores_identity(
    machine, tmp_path, topology
):
    release = retirement_release(tmp_path, topology)
    preview = machine.inspect(release)
    assert preview["retirement"] == [
        {
            "domain": "usr-home",
            "uuid": "uuid-usr-home",
            "private": "usr-home-private",
            "pool_generation": 1,
            "pool_manifest": "/pool/usr-home/1/manifest.json",
            "retention_days": 30,
        }
    ]
    result = machine.apply(release)
    assert result["plan"]["retire"] == ["usr-home"]
    assert "usr-home" not in machine.live
    assert machine.tags["usr-home"] == {"airlock-retired"}
    record = machine.retirement.active_trash()["usr-home"]
    assert record["uuid"] == "uuid-usr-home"
    assert record["storage"]["private"] == "usr-home-private"
    assert record["retention_days"] == 30
    assert any(
        call[:3] == ("qvm-prefs", "usr-home", "autostart") for call in machine.calls
    )
    assert any(
        call[:2] == ("airlock-closure-store", "set-retention") for call in machine.calls
    )

    machine.calls.clear()
    restored = machine.apply(machine.initial, recover=True)
    assert restored["plan"]["restore"] == ["usr-home"]
    assert "usr-home" in machine.live
    assert machine.tags["usr-home"] == set()
    assert machine.retirement.trash_records()[0]["status"] == "restored"
    assert machine.uuids["usr-home"] == "uuid-usr-home"


def test_restore_rejects_private_storage_identity_change(machine, tmp_path, topology):
    machine.apply(retirement_release(tmp_path, topology))
    changed = deepcopy(topology)
    changed["domains"]["usr-home"]["backend"]["state"]["persistent"] = "other"
    restore = write_release(tmp_path / "restore", changed, "restore")
    with pytest.raises(deploy.DeploymentError, match="private storage identity"):
        machine.apply(restore)


def test_boot_enforces_trash_and_refuses_running_retired_vm(
    machine, tmp_path, topology
):
    release = retirement_release(tmp_path, topology)
    machine.apply(release)
    machine.tags["usr-home"].clear()
    machine.boot_start("/missing-image")
    assert machine.tags["usr-home"] == {"airlock-retired"}
    machine.live.add("usr-home")
    with pytest.raises(deploy.DeploymentError, match="has been stopped"):
        machine.boot_start("/missing-image")
    assert "usr-home" not in machine.live


def test_interrupted_retirement_resumes_from_durable_record(
    machine, tmp_path, topology
):
    release = retirement_release(tmp_path, topology)
    machine.fail = "qvm-tags"
    with pytest.raises(deploy.DeploymentError, match="simulated failure"):
        machine.apply(release)
    record = machine.retirement.trash_records()[0]
    assert record["status"] == "retiring"
    assert machine.status()["failed_phase"] == "retiring"

    machine.fail = None
    machine.calls.clear()
    result = machine.apply(release, recover=True)
    assert result["phase"] == "reboot-required"
    assert machine.retirement.active_trash()["usr-home"]["status"] == "trashed"


def test_purge_requires_expiry_and_no_rollback_reference(machine, tmp_path, topology):
    release = retirement_release(tmp_path, topology)
    machine.apply(release)
    record = machine.retirement.active_trash()["usr-home"]
    with pytest.raises(deploy.DeploymentError, match="retention window"):
        machine.retirement.purge_trash(record["id"], record["id"])

    now = record["purge_after"] + 1
    with pytest.raises(deploy.DeploymentError, match="previous deployment"):
        machine.retirement.purge_trash(record["id"], record["id"], now=now)

    selection = machine.pointer()
    selection["previous"] = str(release)
    deploy.atomic_json(machine.state / "selection.json", selection)
    # A release older than the selection pair is still recoverable if rooted.
    roots = machine.state / "roots"
    roots.mkdir(exist_ok=True)
    (roots / "old-release").symlink_to(machine.initial)
    with pytest.raises(deploy.DeploymentError, match="retained deployment"):
        machine.retirement.purge_trash(record["id"], record["id"], now=now)
    # Pruning uses the same controlled clock as purge, without editing expiry.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(deploy.time, "time", lambda: now)
        machine.releases.prune_release(machine.initial, str(machine.initial))
    machine.calls.clear()
    result = machine.retirement.purge_trash(record["id"], record["id"], now=now)
    assert result["status"] == "purged"
    assert "usr-home" not in machine.selected
    assert any(call[0] == "qvm-remove" for call in machine.calls)
    assert any(
        call[:2] == ("airlock-closure-store", "retire-generation")
        for call in machine.calls
    )


def test_exact_early_purge_override_preserves_other_guards(machine, tmp_path, topology):
    retired = retirement_release(tmp_path, topology)
    machine.apply(retired)
    record = machine.retirement.active_trash()["usr-home"]
    identity = record["id"]
    source = str(machine.initial)

    with pytest.raises(deploy.DeploymentError, match="selected previous"):
        machine.releases.prune_release(
            source, source, early_purge_trash=[identity]
        )
    with pytest.raises(deploy.DeploymentError, match="previous deployment"):
        machine.retirement.purge_trash(
            identity, identity, before_expiry=True
        )

    follow = write_release(
        tmp_path / "after-retirement", deploy.load_release(retired)[1], "follow"
    )
    machine.calls.clear()
    machine.apply(follow)
    with pytest.raises(deploy.DeploymentError, match="does not match active trash"):
        machine.releases.prune_plan(
            source, early_purge_trash=["999-other-vm"]
        )
    preview = machine.releases.prune_plan(source, early_purge_trash=[identity])
    assert preview["blockers"] == []
    assert preview["waived_blockers"] == ["unexpired trash " + identity]

    machine.releases.retain_release(source, True)
    with pytest.raises(deploy.DeploymentError, match="explicitly retained"):
        machine.releases.prune_release(
            source, source, early_purge_trash=[identity]
        )
    machine.releases.retain_release(source, False)
    with pytest.raises(deploy.DeploymentError, match="retained deployment"):
        machine.retirement.purge_trash(
            identity, identity, before_expiry=True
        )
    pruned = machine.releases.prune_release(
        source, source, early_purge_trash=[identity]
    )
    assert pruned["status"] == "pruned"
    assert pruned["waived_blockers"] == ["unexpired trash " + identity]
    with pytest.raises(deploy.DeploymentError, match="retention window"):
        machine.retirement.purge_trash(identity, identity)
    purged = machine.retirement.purge_trash(
        identity, identity, before_expiry=True
    )
    assert purged["status"] == "purged"
    assert purged["purged_before_expiry"] is True
    assert machine.retirement.purge_trash(identity, identity) == purged


def test_interrupted_purge_is_retryable(machine, tmp_path, topology):
    release = retirement_release(tmp_path, topology)
    machine.apply(release)
    record = machine.retirement.active_trash()["usr-home"]
    now = record["purge_after"] + 1
    selection = machine.pointer()
    selection["previous"] = str(release)
    deploy.atomic_json(machine.state / "selection.json", selection)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(deploy.time, "time", lambda: now)
        machine.releases.prune_release(machine.initial, str(machine.initial))

    original = machine.execute

    def fail_retire(*args, **kwargs):
        if args[:2] == ("airlock-closure-store", "retire-generation"):
            raise deploy.DeploymentError("simulated failure")
        return original(*args, **kwargs)

    machine.command = fail_retire
    machine.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="simulated failure"):
        machine.retirement.purge_trash(record["id"], record["id"], now=now)
    assert machine.retirement.trash_records()[0]["status"] == "purging"
    assert "usr-home" not in machine.selected

    machine.command = machine.execute
    assert (
        machine.retirement.purge_trash(record["id"], record["id"], now=now)["status"] == "purged"
    )


def test_reapplying_same_release_does_not_lose_rollback(machine, tmp_path, topology):
    release = new_release(tmp_path, topology)
    first = machine.apply(release)
    machine.calls.clear()
    second = machine.apply(release)
    assert second["id"] == first["id"]
    assert second["previous"] == str(machine.initial)
    assert not any(call[0] == "qvm-shutdown" for call in machine.calls)


@pytest.fixture
def obsolete_release(machine, tmp_path, topology):
    releases = [
        write_release(tmp_path / f"history-{i}", topology, f"history-{i}")
        for i in range(3)
    ]
    for release in releases:
        machine.apply(release)
    machine.calls.clear()
    return releases[0]


def test_pruning_plan_is_read_only_and_preserves_current_and_rollback(
    machine, obsolete_release
):
    preview = machine.releases.prune_plan(obsolete_release)
    assert not preview["blockers"]
    assert preview["roots"]
    assert not machine.releases.prune_record_path(obsolete_release).exists()
    for key in ("release", "previous"):
        target = machine.pointer()[key]
        assert "selected " + key in machine.releases.prune_plan(target)["blockers"]
        with pytest.raises(deploy.DeploymentError, match="selected"):
            machine.releases.prune_release(target, target)
    assert not machine.calls


@pytest.mark.parametrize(
    "guard", ["running", "profile", "recovery", "retained", "unfinished"]
)
def test_pruning_protection(machine, obsolete_release, guard):
    host = deploy.load_release(obsolete_release)[0]["host"]
    machine.profiles.mkdir(parents=True)
    if guard == "running":
        machine.hosts.add(host)
    elif guard == "profile":
        (machine.profiles / "system").symlink_to(host)
    elif guard == "recovery":
        path = machine.profiles / "system-profiles/airlock-recovery"
        path.parent.mkdir()
        path.symlink_to(host)
    elif guard == "retained":
        machine.releases.retain_release(obsolete_release, True)
    else:
        deploy.atomic_json(
            machine.state / "transactions/999/status.json",
            {"id": "999", "phase": "failed", "release": str(obsolete_release)},
        )
    assert machine.releases.prune_plan(obsolete_release)["blockers"]
    with pytest.raises(deploy.DeploymentError, match="cannot prune"):
        machine.releases.prune_release(obsolete_release, str(obsolete_release))


def test_explicit_retention_can_be_released(machine, obsolete_release):
    machine.releases.retain_release(obsolete_release, True)
    machine.releases.retain_release(obsolete_release, False)
    assert (
        machine.releases.prune_release(obsolete_release, str(obsolete_release))["status"]
        == "pruned"
    )


def test_prune_resumes_after_profile_deletion_without_touching_other_generations(
    machine, obsolete_release
):
    machine.profiles.mkdir(parents=True)
    host = deploy.load_release(obsolete_release)[0]["host"]
    selected_host = deploy.load_release(machine.pointer()["release"])[0]["host"]
    (machine.profiles / "system-1-link").symlink_to(host)
    (machine.profiles / "system-2-link").symlink_to(selected_host)
    machine.fail = selected_host + "/bin/switch-to-configuration"
    with pytest.raises(deploy.DeploymentError, match="simulated"):
        machine.releases.prune_release(obsolete_release, str(obsolete_release))
    assert not (machine.profiles / "system-1-link").is_symlink()
    assert (machine.profiles / "system-2-link").is_symlink()
    assert machine.releases.prune_plan(obsolete_release)["status"] == "pruning"
    with pytest.raises(deploy.DeploymentError, match="unfinished release pruning"):
        machine.apply(machine.pointer()["release"])
    machine.fail = None
    result = machine.releases.prune_release(obsolete_release, str(obsolete_release))
    assert result["status"] == "pruned"
    assert all(
        not (machine.state / "roots" / name).is_symlink() for name in result["roots"]
    )
    assert len([c for c in machine.calls if "--delete-generations" in c]) == 1
    machine.calls.clear()
    assert machine.releases.prune_release(obsolete_release, str(obsolete_release)) == result
    assert not machine.calls
    with pytest.raises(deploy.DeploymentError, match="pruned"):
        machine.apply(obsolete_release)


def test_prune_rechecks_targets_and_blockers_on_retry(
    machine, obsolete_release, monkeypatch
):
    real_unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path.parent == machine.state / "roots":
            raise OSError("simulated interruption")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError, match="interruption"):
            machine.releases.prune_release(obsolete_release, str(obsolete_release))
    record = machine.releases.prune_plan(obsolete_release)
    root = machine.state / "roots" / next(iter(record["roots"]))
    root.unlink()
    root.symlink_to("/nix/store/unrelated")
    with pytest.raises(deploy.DeploymentError, match="root changed"):
        machine.releases.prune_release(obsolete_release, str(obsolete_release))
    assert root.is_symlink()


def test_prune_retry_survives_collection_of_release_artifact(
    machine, obsolete_release, monkeypatch
):
    original = releases.atomic_json

    def crash(path, data):
        if data.get("status") == "pruned":
            raise OSError("completion record interrupted")
        original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(releases, "atomic_json", crash)
        with pytest.raises(OSError, match="interrupted"):
            machine.releases.prune_release(obsolete_release, str(obsolete_release))
    # Simulate collection of this test artifact after its last root is gone.
    for artifact in obsolete_release.iterdir():
        artifact.unlink()
    obsolete_release.rmdir()
    result = machine.releases.prune_release(obsolete_release, str(obsolete_release))
    assert result["status"] == "pruned"
    assert any(r.get("status") == "pruned" for r in machine.releases.list_releases()["releases"])
    with pytest.raises(deploy.DeploymentError, match="pruned"):
        machine.apply(obsolete_release)


def test_prune_refuses_journal_targets_outside_roots(machine, obsolete_release):
    preview = machine.releases.prune_plan(obsolete_release)
    preview.update(status="pruning", roots={"../selection.json": "/nix/store/other"})
    deploy.atomic_json(machine.releases.prune_record_path(obsolete_release), preview)
    before = machine.pointer()
    with pytest.raises(deploy.DeploymentError, match="invalid release pruning journal"):
        machine.releases.prune_release(obsolete_release, str(obsolete_release))
    assert machine.pointer() == before


@pytest.fixture
def purgeable(machine, tmp_path, topology, monkeypatch):
    retired = retirement_release(tmp_path, topology)
    machine.apply(retired)
    record = machine.retirement.active_trash()["usr-home"]
    # Advance the rollback choice through an ordinary committed release.
    follow = write_release(
        tmp_path / "after-retirement", deploy.load_release(retired)[1], "follow"
    )
    machine.calls.clear()
    machine.apply(follow)
    assert (
        "unexpired trash " + record["id"]
        in machine.releases.prune_plan(machine.initial)["blockers"]
    )
    monkeypatch.setattr(deploy.time, "time", lambda: record["purge_after"] + 1)
    machine.releases.prune_release(machine.initial, str(machine.initial))
    machine.calls.clear()
    return record


@pytest.mark.parametrize(
    "conflict",
    [
        "uuid",
        "volume",
        "shared",
        "source",
        "tag",
        "generation",
        "legacy",
        "missing",
        "running",
    ],
)
def test_purge_checks_identity_and_ownership_before_mutation(
    machine, purgeable, conflict
):
    name = purgeable["domain"]
    if conflict == "uuid":
        machine.uuids[name] = "replacement"
    elif conflict == "volume":
        machine.volumes[name]["private"]["vid"] = "replacement/private"
    elif conflict in ("shared", "source"):
        volume = deepcopy(machine.volumes[name]["private"])
        if conflict == "source":
            volume["source"], volume["vid"] = volume["vid"], "other/private"
        machine.volumes["sys-dev"]["private"] = volume
    elif conflict == "tag":
        machine.tags[name].clear()
    elif conflict == "generation":
        machine.generations[name] = 2
    elif conflict == "legacy":
        del purgeable["storage"]["volumes"]
        deploy.atomic_json(machine.retirement.trash_path(purgeable["id"]), purgeable)
    elif conflict == "missing":
        del machine.selected[name]
    else:
        machine.live.add(name)
    with pytest.raises(deploy.DeploymentError):
        machine.retirement.purge_trash(purgeable["id"], purgeable["id"])
    assert machine.retirement.trash_records()[0]["status"] == "trashed"
    assert not any(c[0] == "qvm-remove" for c in machine.calls)


def test_purge_is_exact_and_idempotent(machine, purgeable):
    with pytest.raises(deploy.DeploymentError, match="confirmation"):
        machine.retirement.purge_trash(purgeable["id"], "another-record")
    survivors = set(machine.selected) - {purgeable["domain"]}
    result = machine.retirement.purge_trash(purgeable["id"], purgeable["id"])
    assert result["status"] == "purged"
    assert set(machine.selected) == survivors
    assert [c for c in machine.calls if c[0] == "qvm-remove"] == [
        ("qvm-remove", "--force", "usr-home")
    ]
    assert not any(
        c[0] in ("rm", "nix-collect-garbage") or "gc" in c for c in machine.calls
    )
    machine.calls.clear()
    assert machine.retirement.purge_trash(purgeable["id"], purgeable["id"]) == result
    assert not machine.calls


def test_purge_protects_unmanaged_boot_profile(machine, purgeable, tmp_path):
    host = tmp_path / "recovery-host"
    deploy.atomic_json(
        host / "etc/airlock/qubes-topology.json",
        deploy.load_release(machine.initial)[1],
    )
    machine.profiles.mkdir(parents=True)
    (machine.profiles / "system-77-link").symlink_to(host)
    with pytest.raises(deploy.DeploymentError, match="retained host profile"):
        machine.retirement.purge_trash(purgeable["id"], purgeable["id"])


def test_retention_starts_when_retirement_finishes(
    machine, tmp_path, topology, monkeypatch
):
    monkeypatch.setattr(deploy.time, "time", lambda: 100)
    record = machine.retirement.prepare_retirement(
        "usr-home", topology["domains"]["usr-home"], machine.initial, "123", 30
    )
    monkeypatch.setattr(deploy.time, "time", lambda: 1000)
    result = machine.retirement.retire(record)
    assert result["purge_after"] == 1000 + 30 * 86400
