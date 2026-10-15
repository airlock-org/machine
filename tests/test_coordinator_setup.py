"""Connect real coordinator staging to real setup with inert Qubes/pool commands."""

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))
coordinator = importlib.import_module("airlock_machine_deploy.coordinator")


@pytest.fixture
def staged_machine(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    pool = {}
    domains = {}
    features = {}
    artifact = {
        "schema": "airlock.root-artifact.v1",
        "sha256": "a" * 64,
        "logical_bytes": 16777216,
        "payload_bytes": 65576,
        "format": "ext4",
        "boot_abi": "airlock.nix-closure-root.v1",
    }
    for name in ("sys-ctrl", "usr-assistant"):
        source = str(tmp_path / "controller-only" / name)
        root = str(tmp_path / "missing-roots" / name / "root.img")
        system = "/nix/store/" + "a" * 32 + "-" + name
        store = {
            "mode": "shared-closure",
            "source": source,
            "device": "/dev/xvde",
            "blobDevice": "/dev/xvdf",
            "blob_device": "/dev/xvdf",
            "viewBackend": "erofs-objectmap",
            "view_backend": "erofs-objectmap",
            "mountpoint": "/nix/store",
            "read_only": True,
        }
        domains[name] = {
            "class": "StandaloneVM",
            "label": "green",
            "template": None,
            "netvm": None,
            "prefs": {},
            "features": {},
            "services": {},
            "devices": {"pci": []},
            "backend": {
                "type": "nix-closure",
                "system": {"source": system},
                "kernel": {"name": "kernel"},
                "root": {"source": root},
                "store": store,
                "state": {},
            },
        }
        pool[name] = {
            "schema": coordinator.GENERATION_INFO_SCHEMA,
            "status": "passed",
            "domain": name,
            "generation": 2,
            "system": system,
            "root_source": root,
            "root_artifact": artifact,
            "kernel_name": "kernel",
            "closure_source": source,
            "store": {
                "mode": "shared-closure",
                "mountpoint": "/nix/store",
                "device": "/dev/xvde",
                "blob_device": "/dev/xvdf",
                "view_backend": "erofs-objectmap",
            },
            "approval": {
                "approved_by": "sys-ctrl",
                "transport": "qrexec",
                "source": source,
            },
            "provenance": {"source_revision": "b" * 40, "lock_hash": "c" * 64},
            "verification": {"status": "passed"},
            "retention": {"retain": True},
        }
        features[name] = {
            "airlock-root-source": root,
            "airlock-root-artifact": artifact["sha256"],
            "airlock-store-source": source if name == "sys-ctrl" else "/old-closure",
            "airlock-store-mode": "shared-closure",
        }
    topology = {
        "schema": "airlock.qubes-topology.v1",
        "domains": domains,
        "templates": {},
        "kernels": {},
        "prefs": {},
        "qrexec_policy": {"rules": []},
    }
    manifest = {
        "schema": coordinator.SCHEMA,
        "profile": "usb-lab",
        "host": "/nix/store/host",
        "revision": "b" * 40,
        "lockHash": "c" * 64,
        "setup": str(ROOT / "lifecycle/misc/tools/scripts/setup-config.sh"),
        "stagedGenerations": {name: 2 for name in domains},
        "guestRootArtifacts": {name: artifact for name in domains},
    }
    coordinator.atomic_json(release / "release.json", manifest)
    coordinator.atomic_json(release / "topology.json", topology)
    coordinator.atomic_json(tmp_path / "pool.json", pool)
    coordinator.atomic_json(tmp_path / "features.json", features)
    (tmp_path / "capabilities").write_text("control_d\n")
    (tmp_path / "root.img").write_bytes(b"retained root")
    for name in (
        "airlock-closure-store",
        "airlock-nix-domain-storage",
        "qvm-ls",
        "qvm-features",
        "qvm-prefs",
        "qvm-volume",
        "qvm-service",
        "qvm-create",
        "qubes-prefs",
        "qubesd-query",
        "qvm-template-postprocess",
        "systemctl",
    ):
        path = commands / name
        path.write_text(f"#!{sys.executable}\n" + QUBES_COMMAND)
        path.chmod(0o755)
    environment = dict(
        PATH=f"{commands}:{os.environ['PATH']}",
        TEST_STATE=str(tmp_path),
        AIRLOCK_XEN_CAPABILITIES=str(tmp_path / "capabilities"),
        AIRLOCK_QUBES_POLICY_DIR=str(tmp_path / "policy"),
        AIRLOCK_SYSTEM_STATE_DIR=str(tmp_path / "system"),
        AIRLOCK_QUBES_KERNEL_BASE=str(tmp_path / "kernels"),
        AIRLOCK_NIX_ROOT="",
        AIRLOCK_USE_HOST_NIX_STORE="1",
    )

    def command(*args, **kwargs):
        env = dict(os.environ, **kwargs.pop("env", {}))
        env.update(environment)
        actual = ["bash", *args] if args[0] == manifest["setup"] else list(args)
        return subprocess.check_output(actual, env=env, text=True, **kwargs).strip()

    machine = coordinator.Coordinator(tmp_path / "machine", command)
    return machine, release, topology, transaction, tmp_path


QUBES_COMMAND = r"""
import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_STATE'])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
pool = json.loads((root / 'pool.json').read_text())
features = json.loads((root / 'features.json').read_text())
if name == 'airlock-closure-store':
    domain = args[args.index('--domain') + 1]
    if args[0] == 'describe-generation': print(json.dumps(pool[domain]))
    elif args[0] == 'resolve-root': print(json.dumps({'image':str(root / 'root.img'), 'artifact':pool[domain]['root_artifact']}))
    else: raise AssertionError('unexpected pool mutation: ' + str(args))
elif name == 'qvm-ls':
    fields = args[args.index('--fields') + 1]
    for domain in pool:
        print(domain + ('|StandaloneVM' if 'CLASS' in fields else '|Halted' if 'STATE' in fields else ''))
elif name in ('qvm-features', 'qvm-prefs', 'qvm-service'):
    if len(args) == 2:
        print(features[args[0]].get(args[1], ''))
    else:
        features[args[0]][args[1]] = args[2]
        (root / 'features.json').write_text(json.dumps(features))
elif name == 'airlock-nix-domain-storage':
    assert args[0] == 'ensure-store', args
    staged = json.loads(Path(os.environ['AIRLOCK_STAGED_GENERATIONS']).read_text())
    assert staged[args[1]] == 2
    print(json.dumps({'generation':2, 'metadata_changed':False, 'volumes_created':[], 'view_created':False}))
elif name == 'systemctl': pass
elif name == 'qubes-prefs':
    if len(args) == 1: print('')
else:
    raise AssertionError('unexpected Qubes mutation: ' + name + ' ' + str(args))
"""


def test_staging_preserves_unchanged_bindings_for_real_setup(staged_machine):
    machine, release, topology, transaction, state = staged_machine
    machine.stage(release, topology, ["usr-assistant"], transaction)
    assert coordinator.read_json(transaction / "staged.json") == {
        "sys-ctrl": 2,
        "usr-assistant": 2,
    }
    machine.setup(release, transaction)
    report = coordinator.read_json(transaction / "setup/setup-config.json")
    assert report["status"] == "passed"
    assert report["root_images_changed"] == 0
    features = coordinator.read_json(state / "features.json")
    assert (
        features["usr-assistant"]["airlock-system-source"]
        == topology["domains"]["usr-assistant"]["backend"]["system"]["source"]
    )
    assert (
        features["sys-ctrl"]["airlock-store-source"]
        == topology["domains"]["sys-ctrl"]["backend"]["store"]["source"]
    )
    assert not (state / "controller-only").exists()


def test_real_setup_rejects_missing_unchanged_binding(staged_machine):
    machine, release, topology, transaction, state = staged_machine
    machine.stage(release, topology, ["usr-assistant"], transaction)
    coordinator.atomic_json(transaction / "staged.json", {"usr-assistant": 2})
    with pytest.raises(subprocess.CalledProcessError):
        machine.setup(release, transaction)
    report = coordinator.read_json(transaction / "setup/setup-config.json")
    assert report["status"] == "failed"
    assert (
        "shared closure source is not a directory"
        in (transaction / "setup/setup-config.commands.log").read_text()
    )
    assert (
        "airlock-system-source"
        not in coordinator.read_json(state / "features.json")["usr-assistant"]
    )


@pytest.mark.parametrize("field", ["system", "provenance"])
def test_staging_rejects_bad_retained_identity_before_setup(staged_machine, field):
    machine, release, topology, transaction, state = staged_machine
    pool = coordinator.read_json(state / "pool.json")
    pool["sys-ctrl"][field] = (
        "/wrong" if field == "system" else {"source_revision": "f" * 40}
    )
    coordinator.atomic_json(state / "pool.json", pool)
    before = (state / "features.json").read_bytes()
    with pytest.raises(coordinator.DeploymentError, match="mismatch"):
        machine.stage(release, topology, ["usr-assistant"], transaction)
    assert (state / "features.json").read_bytes() == before
    assert not (transaction / "setup").exists()
