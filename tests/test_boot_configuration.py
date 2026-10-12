"""Boot reuse and provisioning boundaries, without Xen or a running Qubes host."""

from copy import deepcopy
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))
boot = importlib.import_module("airlock_machine_deploy.boot")


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    topology = {
        "schema": "airlock.qubes-topology.v1",
        "prefs": {"default_guivm": "sys-gui"},
        "kernels": {},
        "templates": {},
        "domains": {
            name: {
                "netvm": None,
                "prefs": {
                    "autostart": name != "sys-gui",
                    "guivm": None if name == "sys-gui" else "sys-gui",
                },
                "features": {"airlock-managed": "1"},
                "backend": {"type": "nix-closure"},
            }
            for name in ("sys-gui", "usr-assistant", "usr-home")
        },
    }
    xml = tmp_path / "qubes.xml"
    tree = ET.Element("qubes")
    ET.SubElement(tree, "labels")
    pools = ET.SubElement(tree, "pools")
    ET.SubElement(pools, "pool", name="file", driver="file", dir_path=str(tmp_path))
    ET.SubElement(
        pools,
        "pool",
        name="closure",
        driver="airlock-closure",
        root_dir=str(tmp_path / "closure"),
    )
    ET.SubElement(tree, "properties")
    ET.SubElement(tree, "domains")
    ET.ElementTree(tree).write(xml)
    policy = tmp_path / "90-airlock.policy"
    policy.write_text("# Managed by Airlock setup-config.\n")
    monkeypatch.setenv("AIRLOCK_QUBES_XML", str(xml))
    monkeypatch.setenv("AIRLOCK_QUBES_POLICY_DIR", str(tmp_path))
    monkeypatch.setenv("AIRLOCK_SYSTEM_STATE_DIR", str(tmp_path / "system"))
    monkeypatch.setenv("AIRLOCK_STARTUP_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("AIRLOCK_LOG_DIR", str(tmp_path / "logs"))
    calls = []

    def provision(name):
        current = ET.parse(xml)
        domains = current.getroot().find("domains")
        if any(
            d.findtext("properties/property[@name='name']") == name for d in domains
        ):
            return
        domain = ET.SubElement(domains, "domain", {"class": "StandaloneVM"})
        props = ET.SubElement(domain, "properties")
        ET.SubElement(props, "property", name="name").text = name
        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "feature", name="airlock-managed").text = "1"
        ET.SubElement(domain, "tags")
        volumes = ET.SubElement(domain, "volume-config")
        for kind in ("root", "private"):
            (tmp_path / f"{name}-{kind}.img").write_bytes(b"original data")
            ET.SubElement(
                volumes,
                "volume",
                name=kind,
                pool="file",
                vid=f"{name}-{kind}",
                size="13",
                rw="True",
            )
        for kind in ("store", "store-data"):
            path = tmp_path / f"{name}-{kind}"
            path.write_bytes(b"immutable data")
            ET.SubElement(
                volumes, "volume", name=kind, pool="closure", view_path=str(path)
            )
        current.write(xml)

    def command(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "setup":
            scoped = boot.read_json(args[-1])
            for name in scoped["domains"]:
                provision(name)
            boot.atomic_json(
                tmp_path / "logs/setup-config.json",
                {
                    "schema": "airlock.setup-config.v1",
                    "status": "passed",
                    "domains_changed": len(scoped["domains"]),
                    "restart_required": False,
                    "unsupported_domains_count": 0,
                },
            )
        return ""

    coordinator = SimpleNamespace(
        state=tmp_path / "machine", command=command, pointer=lambda: None
    )
    manager = boot.BootConfiguration(coordinator)
    monkeypatch.setattr(
        manager,
        "selection",
        lambda *_: ("setup", topology, None, boot.digest(topology)),
    )
    return SimpleNamespace(
        manager=manager,
        topology=topology,
        xml=xml,
        calls=calls,
        policy=policy,
        root=tmp_path,
        provision=provision,
    )


def setup_calls(configuration):
    return [args for args, _ in configuration.calls if args[0] == "setup"]


def test_initialization_prepares_gui_before_unrelated_autostart_vms(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    assert c.manager.gui_domains(c.topology) == ["sys-gui"]
    assert set(boot.read_json(setup_calls(c)[0][-1])["domains"]) == {"sys-gui"}
    assert not (c.manager.system / "current-topology.json").exists()
    c.manager.configure("setup", ["--apply"], gui=False)
    assert set(boot.read_json(setup_calls(c)[1][-1])["domains"]) == {
        "usr-assistant",
        "usr-home",
    }
    assert boot.read_json(c.manager.system / "current-topology.json") == c.topology
    assert (
        boot.read_json(c.manager.system / "current-setup.json")["domains_changed"] == 3
    )


def test_unchanged_boot_skips_setup_but_checks_qubes_services(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    c.manager.configure("setup", ["--apply"], gui=False)
    c.calls.clear()
    c.manager.configure("setup", ["--apply"], gui=True)
    c.manager.configure("setup", ["--apply"], gui=False)
    assert not setup_calls(c)
    assert len([args for args, _ in c.calls if args[0] == "systemctl"]) == 3
    assert boot.read_json(c.root / "logs/boot-configuration.json")["reused"] is True


def test_gui_scope_includes_network_dependencies_not_all_autostart(configuration):
    topology = deepcopy(configuration.topology)
    topology["domains"]["sys-gui"]["netvm"] = "sys-firewall"
    topology["domains"]["sys-firewall"] = {"netvm": "sys-net", "prefs": {"guivm": None}}
    topology["domains"]["sys-net"] = {"netvm": None, "prefs": {"guivm": None}}
    assert configuration.manager.gui_domains(topology) == [
        "sys-net",
        "sys-firewall",
        "sys-gui",
    ]


@pytest.mark.parametrize("changed", ["policy", "store", "configuration"])
def test_configuration_or_artifact_changes_invalidate_reuse(configuration, changed):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    if changed == "policy":
        c.policy.write_text("changed rules\n")
    elif changed == "store":
        (c.root / "sys-gui-store").write_bytes(b"changed immutable data")
    else:
        c.topology["domains"]["sys-gui"]["prefs"]["memory"] = 2048
    assert not c.manager.cached(boot.digest(c.topology), c.topology, ["sys-gui"])


@pytest.mark.parametrize("identity", ["same", "changed"])
def test_missing_private_data_requires_recovery_even_during_update(
    configuration, identity
):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    (c.root / "sys-gui-private.img").unlink()
    wanted = boot.digest(c.topology) if identity == "same" else "new configuration"
    with pytest.raises(boot.DeploymentError, match="private disk is missing"):
        c.manager.cached(wanted, c.topology, ["sys-gui"])


def test_user_data_and_guest_runtime_features_do_not_repeat_setup(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    private = c.root / "sys-gui-private.img"
    private.write_bytes(b"modified data")
    tree = ET.parse(c.xml)
    ET.SubElement(
        tree.getroot().find("domains/domain/features"),
        "feature",
        name="last-shutdown-time",
    ).text = "later"
    tree.write(c.xml)
    assert c.manager.cached(boot.digest(c.topology), c.topology, ["sys-gui"])


def test_background_failure_does_not_claim_full_configuration(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    c.manager.coordinator.command = fail
    with pytest.raises(subprocess.CalledProcessError):
        c.manager.configure("setup", ["--apply"], gui=False)
    assert not (c.manager.system / "current-topology.json").exists()
    assert set(boot.read_json(c.manager.receipt)["domains"]) == {"sys-gui"}


def test_interrupted_transaction_cannot_fall_back_to_image(configuration):
    c = configuration
    manager = boot.BootConfiguration(c.manager.coordinator)
    boot.atomic_json(manager.coordinator.state / "latest.json", {"phase": "failed"})
    with pytest.raises(boot.DeploymentError, match="interrupted deployment"):
        manager.selection("seed-setup", "missing-seed-topology")


def test_background_refuses_configuration_changed_after_gui_start(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    c.topology["domains"]["sys-gui"]["prefs"]["memory"] = 2048
    with pytest.raises(boot.DeploymentError, match="changed after startup"):
        c.manager.configure("setup", ["--apply"], gui=False)
    assert len(setup_calls(c)) == 1


def test_unknown_receipt_schema_is_not_treated_as_first_boot(configuration):
    c = configuration
    boot.atomic_json(c.manager.receipt, {"schema": "unsupported"})
    with pytest.raises(boot.DeploymentError, match="unsupported applied boot"):
        c.manager.configure("setup", ["--apply"], gui=True)
    assert not setup_calls(c)


def test_gui_inherits_default_netvm_when_not_explicitly_disabled(configuration):
    topology = deepcopy(configuration.topology)
    topology["domains"]["sys-gui"].pop("netvm")
    topology["domains"]["sys-net"] = {"netvm": None, "prefs": {"guivm": None}}
    topology["prefs"]["default_netvm"] = "sys-net"
    assert set(configuration.manager.gui_domains(topology)) == {"sys-net", "sys-gui"}


def test_foreign_volume_cannot_alias_gui_private_disk(configuration):
    c = configuration
    c.manager.configure("setup", ["--apply"], gui=True)
    c.provision("usr-assistant")
    tree = ET.parse(c.xml)
    foreign = next(
        d
        for d in tree.getroot().find("domains")
        if d.findtext("properties/property[@name='name']") == "usr-assistant"
    ).find("volume-config")
    ET.SubElement(
        foreign, "volume", name="stolen", pool="file", source="sys-gui-private"
    )
    tree.write(c.xml)
    with pytest.raises(boot.DeploymentError, match="persistent disk"):
        c.manager.cached(boot.digest(c.topology), c.topology, ["sys-gui"])
