"""Concurrent state publication and ownership of machine operations."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle/runtime/tools"))
storage = importlib.import_module("airlock_state")
coordinator = importlib.import_module("airlock_machine_deploy.coordinator")
cli = importlib.import_module("airlock_machine_deploy.cli")


def test_concurrent_publication_never_shares_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    barrier = threading.Barrier(2)
    replace = Path.replace
    temporary_paths = []

    def publish(temporary, destination):
        temporary_paths.append(temporary)
        barrier.wait(timeout=5)
        return replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", publish)
    records = [{"status": phase, "payload": phase * 1000} for phase in ("building", "planned")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: storage.atomic_json(path, value), records))
    assert len(set(temporary_paths)) == 2
    assert json.loads(path.read_text()) in records
    assert list(tmp_path.iterdir()) == [path]


def test_failed_publication_preserves_old_record_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    storage.atomic_json(path, {"status": "building"})

    def fail(*_):
        raise OSError("injected rename failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        storage.atomic_json(path, {"status": "planned"})
    assert json.loads(path.read_text()) == {"status": "building"}
    assert list(tmp_path.iterdir()) == [path]


def test_busy_lock_names_actual_owner_and_reason_without_overwriting_it(tmp_path):
    path = tmp_path / "lock"
    with storage.NamedLock(path, owner="Deployment request abc123", reason="Activate approved changes",
                           operation="apply", operation_id="abc123") as held:
        held.update(phase="configuring")
        with pytest.raises(storage.LockBusy, match="Deployment request abc123.*Activate approved changes") as error:
            with storage.NamedLock(path, owner="Maintenance", reason="Remove unused artifacts", operation="gc"):
                pytest.fail("second writer acquired the lock")
        details = error.value.holder
        assert details["pid"] == os.getpid()
        assert details["operation_id"] == "abc123"
        assert details["phase"] == "configuring"
        assert details["elapsed_seconds"] >= 0
        assert storage.lock_status(path)["owner"] == "Deployment request abc123"
    assert storage.lock_status(path) is None
    assert not held.metadata.exists()


def _hold_lock(path, ready, finish):
    with storage.NamedLock(path, owner="Automatic maintenance", reason="Collect unused pool artifacts",
                           operation="generation-maintenance"):
        ready.set()
        finish.wait(10)


@pytest.mark.parametrize("crash", [False, True])
def test_exited_owner_cannot_leave_a_permanent_busy_lock(tmp_path, crash):
    path = tmp_path / "lock"
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(path, ready, finish))
    process.start()
    try:
        assert ready.wait(5)
        assert storage.lock_status(path)["pid"] == process.pid
        if crash:
            process.kill()
        else:
            finish.set()
        process.join(5)
        assert not process.is_alive()
        assert storage.lock_status(path) is None
        with storage.NamedLock(path, owner="Next deployment", reason="Activate a new generation", operation="apply"):
            assert storage.lock_status(path)["owner"] == "Next deployment"
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        process.close()


@pytest.mark.parametrize("metadata", ["broken json", '{"pid":999999999}', '{}'])
def test_missing_owner_details_do_not_bypass_kernel_lock(tmp_path, metadata):
    path = tmp_path / "lock"
    path.write_text("")
    path.with_name("lock.owner.json").write_text(metadata)
    with path.open() as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert storage.lock_status(path)["owner"] == "Unknown operation"
        with pytest.raises(storage.LockBusy):
            with storage.NamedLock(path, owner="Contender", reason="Attempt another operation", operation="apply"):
                pytest.fail("metadata replaced the kernel lock")
    assert storage.lock_status(path) is None


def test_lock_observation_does_not_create_state(tmp_path):
    assert storage.lock_status(tmp_path / "missing" / "lock") is None
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("field", ["boot_id", "process_start"])
def test_previous_boot_or_reused_pid_metadata_is_not_reported_as_current_owner(tmp_path, field):
    path = tmp_path / "lock"
    with storage.NamedLock(path, owner="Actual operation", reason="Hold the resource", operation="apply") as held:
        storage.atomic_json(held.metadata, {**held.info, field: "stale"})
        assert storage.lock_status(path)["owner"] == "Unknown operation"
    assert storage.lock_status(path) is None


@pytest.mark.parametrize("operation", ["status", "check"])
def test_review_status_remains_readable_during_maintenance_but_checks_stay_locked(tmp_path, monkeypatch, capsys, operation):
    machine = coordinator.Coordinator(tmp_path)
    identity = "a" * 64
    record = {"request_id": identity, "status": "approved"}
    storage.atomic_json(tmp_path / "reviews/requests" / (identity + ".json"), record)
    request = {"op": operation, "request_id": identity}
    if operation == "check":
        request.update(revision="b" * 40, lock_hash="c" * 64)
    monkeypatch.setattr(cli, "Coordinator", lambda: machine)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode() + b"\n")))
    with machine.locked("Automatic maintenance", "Collect unused pool artifacts", "generation-maintenance"):
        code = cli.main(["review-control"])
        reply = json.loads(capsys.readouterr().out)
        assert reply["lock"]["owner"] == "Automatic maintenance"
        assert reply["lock"]["reason"] == "Collect unused pool artifacts"
        assert machine.status()["lock"]["operation"] == "generation-maintenance"
        if operation == "status":
            assert code == 0
            assert reply["status"] == "approved"
        else:
            assert code == 1
            assert reply["status"] == "busy"
            assert "Automatic maintenance" in reply["error"]
    assert json.loads((tmp_path / "reviews/requests" / (identity + ".json")).read_text()) == record
