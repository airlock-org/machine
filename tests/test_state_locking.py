"""Concurrent state publication and ownership of machine operations."""

from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle/runtime/tools"))
storage = importlib.import_module("airlock_state")


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
