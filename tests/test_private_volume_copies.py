"""Replacement data stays independent and is never overwritten on resume."""

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle/runtime/tools"))
copies = importlib.import_module("airlock_machine_deploy.private_copies")
validation = importlib.import_module("airlock_machine_deploy.validation")


class Volume:
    pool = "private"
    rw = True
    save_on_stop = True
    snap_on_start = False
    source = None
    size = 1024

    def __init__(self, name):
        self.vid = name
        self.data = name.encode()
        self.calls = 0
        self.fail = False

    def clone(self, source):
        self.calls += 1
        if self.fail:
            raise RuntimeError("interrupted clone")
        self.data = source.data


class VM:
    def __init__(self, name):
        self.name = name
        self.uuid = "uuid-" + name
        self.volumes = {"private": Volume(name)}
        self.running = False

    def is_running(self):
        return self.running


def topology(name):
    return {"schema": "airlock.qubes-topology.v1", "domains": {name: {
        "class": "StandaloneVM", "backend": {
            "type": "nix-closure", "state": {"persistent": name + "-private"}}}}}


@pytest.fixture
def replacement(tmp_path, monkeypatch):
    manager = copies.PrivateVolumeCopies(SimpleNamespace(state=tmp_path))
    source = VM("old")
    target = VM("new")
    domains = {"old": source}
    monkeypatch.setattr(manager, "app", lambda: SimpleNamespace(domains=domains))
    old, new = topology("old"), topology("new")
    entries = manager.inspect({"new": "old"}, old, new)
    manager.prepare(entries)
    domains["new"] = target
    return manager, source, target, entries, old, new


def test_completed_copy_preserves_new_writes_across_resume(replacement):
    manager, source, target, entries, old, new = replacement
    manager.apply(entries)
    assert target.volumes["private"].data == source.volumes["private"].data
    target.volumes["private"].data = b"new personal files"
    target.running = True
    resumed = manager.inspect({"new": "old"}, old, new)
    manager.apply(resumed)
    assert target.volumes["private"].data == b"new personal files"
    assert target.volumes["private"].calls == 1
    assert source.volumes["private"].data == b"old"


def test_failed_clone_remains_resumable_before_first_start(replacement):
    manager, _, target, entries, old, new = replacement
    target.volumes["private"].fail = True
    with pytest.raises(RuntimeError, match="interrupted clone"):
        manager.apply(entries)
    target.volumes["private"].fail = False
    resumed = manager.inspect({"new": "old"}, old, new)
    assert resumed[0]["status"] == "copying"
    manager.apply(resumed)
    assert target.volumes["private"].data == b"old"


@pytest.mark.parametrize("change, message", [
    ("source-running", "both VMs stopped"),
    ("target-running", "both VMs stopped"),
    ("smaller", "smaller than source"),
    ("source-uuid", "identity changed"),
    ("shared", "independent persistent"),
])
def test_copy_fails_closed_on_unsafe_state(replacement, change, message):
    manager, source, target, entries, _, _ = replacement
    if change == "source-running":
        source.running = True
    elif change == "target-running":
        target.running = True
    elif change == "smaller":
        target.volumes["private"].size = 1
    elif change == "source-uuid":
        source.uuid = "changed"
    else:
        target.volumes["private"].source = "another-volume"
    with pytest.raises(copies.DeploymentError, match=message):
        manager.apply(entries)
    assert target.volumes["private"].calls == 0


def test_preexisting_vm_is_never_adopted_for_a_copy(tmp_path, monkeypatch):
    manager = copies.PrivateVolumeCopies(SimpleNamespace(state=tmp_path))
    monkeypatch.setattr(manager, "app", lambda: SimpleNamespace(domains={
        "old": VM("old"), "new": VM("new")}))
    with pytest.raises(copies.DeploymentError, match="overwrite an existing VM"):
        manager.inspect({"new": "old"}, topology("old"), topology("new"))
    assert not manager.path("new").exists()


def test_replacement_volume_identity_cannot_change_after_copy(replacement):
    manager, _, target, entries, old, new = replacement
    manager.apply(entries)
    target.uuid = "different-vm"
    with pytest.raises(copies.DeploymentError, match="target identity changed"):
        manager.inspect({"new": "old"}, old, new)


@pytest.mark.parametrize("mapping", [None, [], {"new": "new"},
    {"missing": "old"}, {"new": "unretired"}, {"../escape": "old"}])
def test_copy_declaration_requires_new_target_and_retired_source(mapping):
    with pytest.raises(copies.DeploymentError, match="copy map"):
        validation.validate_release_topology({"retireDomains": ["old"],
            "privateVolumeCopies": mapping}, topology("new"))
