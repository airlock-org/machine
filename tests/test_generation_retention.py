"""Retention selection regressions; these do not invoke storage maintenance."""

import contextlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))

from airlock_machine_deploy import generation_retention as retention
from airlock_machine_deploy.state import atomic_json, read_json


def test_newest_two_and_closest_week_old_generation():
    now = 20 * retention.WEEK
    history = {str(age): {"first_verified_at": now - age * 86400}
               for age in (0, 1, 3, 7, 8, 14)}
    assert retention.retained_keys(history, now) == {"0", "1", "3", "7"}


def test_oldest_verified_fallback_until_a_week_exists():
    now = 20 * retention.WEEK
    history = {str(age): {"first_verified_at": now - age * 86400} for age in (0, 1, 2, 3)}
    assert retention.retained_keys(history, now) == {"0", "1", "2", "3"}
    assert retention.retained_keys({}, now) == set()


def test_frequent_updates_rotate_weekly_anchor_with_at_most_four_choices():
    history = {}
    anchors = []
    for day in range(30):
        now = 100 * retention.WEEK + day * 86400
        history[str(day)] = {"first_verified_at": now}
        keep = retention.retained_keys(history, now)
        assert len(keep) <= 4
        history = {key: value for key, value in history.items() if key in keep}
        weekly = [value["first_verified_at"] for value in history.values()
                  if value["first_verified_at"] <= now - retention.WEEK]
        if weekly:
            anchors.append(max(weekly))
            assert retention.WEEK <= now - max(weekly) < 2 * retention.WEEK
    assert len(set(anchors)) > 2


def test_machine_snapshot_deduplicates_aliases_but_not_guest_changes():
    manifest = {"host": "/nix/store/host", "setup": "/nix/store/setup", "revision": "first"}
    topology = {"domains": {"usr-home": {"label": "blue"}}}
    key = retention.generation_key(manifest, topology)
    assert key == retention.generation_key({**manifest, "revision": "alias"}, topology)
    assert key != retention.generation_key(manifest, {"domains": {"usr-home": {"label": "khaki"}}})


def machine(tmp_path):
    return SimpleNamespace(
        state=tmp_path,
        releases=SimpleNamespace(prune_record_path=lambda release: tmp_path / (Path(release).name + ".pruned")),
    )


def test_reboot_required_does_not_queue_cleanup(tmp_path, monkeypatch):
    manager = retention.GenerationRetention(machine(tmp_path))
    monkeypatch.setattr(manager, "enabled", lambda: True)
    manager.record_verified({"phase": "reboot-required"})
    assert not manager.directory.exists()


def test_repeat_verification_preserves_age_and_completed_cleanup(tmp_path, monkeypatch):
    manager = retention.GenerationRetention(machine(tmp_path))
    monkeypatch.setattr(manager, "enabled", lambda: True)
    manifest = {"host": "/nix/store/host", "setup": "/nix/store/setup"}
    topology = {"domains": {}}
    monkeypatch.setattr(retention, "load_release", lambda release: (manifest, topology))
    record = {"phase": "complete", "release": "/candidate", "host_running": manifest["host"],
              "generation_verified_at": 100}
    manager.record_verified(record)
    key = retention.generation_key(manifest, topology)
    assert read_json(manager.directory / "verified-generations.json")[key]["first_verified_at"] == 100
    atomic_json(manager.directory / "last-cleaned-generation.json", {"key": key})
    (manager.directory / "pending.json").unlink()
    manager.record_verified(record)
    assert not (manager.directory / "pending.json").exists()


@pytest.mark.parametrize("manual, referenced", [(True, False), (False, True), (False, False)])
def test_shared_or_manual_storage_retains_are_preserved(tmp_path, monkeypatch, manual, referenced):
    owner = machine(tmp_path)
    owner.releases.release_inventory = lambda: ["/survivor"] if referenced else []
    calls = []
    owner.command = lambda *args: calls.append(args)
    manager = retention.GenerationRetention(owner)
    monkeypatch.setattr(retention, "POOL", tmp_path / "pool")
    monkeypatch.setattr(retention, "load_release", lambda release: (
        {"stagedGenerations": {"usr-home": 3}}, {"domains": {"usr-home": {}}},
    ))
    path = retention.POOL / "domains/usr-home/generations/3/manifest.json"
    flags = {"retain": True, **({"updated_at": "manual"} if manual else {})}
    atomic_json(path, {"system": "/nix/store/guest", "retention": flags})
    manager.release_storage_retention([["usr-home", 3]])
    assert bool(calls) is (not manual and not referenced)


def test_failed_gc_preserves_pending_request_for_retry(tmp_path, monkeypatch):
    owner = machine(tmp_path)
    owner.locked = contextlib.nullcontext
    owner.pointer = lambda: {"transaction": "1", "release": "/candidate"}
    owner.command = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("GC failed"))
    manager = retention.GenerationRetention(owner)
    monkeypatch.setattr(manager, "enabled", lambda: True)
    manifest = {"host": str(Path("/run/current-system").resolve()), "setup": "/setup"}
    topology = {"domains": {}}
    monkeypatch.setattr(retention, "load_release", lambda release: (manifest, topology))
    monkeypatch.setattr(manager, "plan", lambda: {"prune": [], "storage_candidates": []})
    monkeypatch.setattr(manager, "release_storage_retention", lambda candidates: [])
    atomic_json(owner.state / "transactions/1/status.json", {"phase": "complete"})
    atomic_json(manager.directory / "pending.json", {"key": retention.generation_key(manifest, topology)})
    with pytest.raises(RuntimeError, match="GC failed"):
        manager.run()
    assert (manager.directory / "pending.json").exists()
    assert read_json(manager.directory / "latest.json")["status"] == "failed"
