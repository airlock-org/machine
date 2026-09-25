import importlib.util
import io
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_ctrl_machine_receive", ROOT / "lifecycle/runtime/tools/sys-ctrl-machine-receive.py"
)
RECEIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIVER)


@pytest.mark.parametrize("change", [
    {"schema": "unknown"},
    {"release": "/tmp/release"},
    {"revision": "bad"},
    {"profile": "other"},
    {"paths": {"/tmp/invalid": 1}},
])
def test_invalid_release_header_fails_before_import(monkeypatch, change):
    header = {"schema": RECEIVER.SCHEMA,
              "release": "/nix/store/" + "a" * 32 + "-release",
              "revision": "b" * 40, "lock_hash": "c" * 64,
              "profile": "usb-lab", "paths": {"/nix/store/" + "a" * 32 + "-release": 0}}
    header.update(change)
    monkeypatch.setattr(RECEIVER.sys, "stdin", io.TextIOWrapper(
        io.BytesIO((json.dumps(header) + "\n").encode())))
    monkeypatch.setattr(RECEIVER.subprocess, "Popen", lambda *_args, **_kwargs:
                        pytest.fail("invalid input reached Nix import"))
    with pytest.raises(ValueError):
        RECEIVER.receive("plan")


@pytest.mark.parametrize("action", ["plan", "apply"])
def test_imported_release_is_checked_before_coordinator_action(monkeypatch, tmp_path, capsys, action):
    release = "/nix/store/" + "a" * 32 + "-release"
    coordinator = "/nix/store/" + "d" * 32 + "-coordinator/bin/airlock-machine-deploy"
    header = {"schema": RECEIVER.SCHEMA, "release": release,
              "revision": "b" * 40, "lock_hash": "c" * 64,
              "profile": "usb-lab", "paths": {release: 4}, "receipts": {}}
    manifest = {"schema": "airlock.machine-release.v1", "revision": "b" * 40,
                "lockHash": "c" * 64, "profile": "usb-lab", "coordinator": coordinator,
                "guestTransport": "direct-pool-v1"}
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    wire = json.dumps(header).encode() + b"\n" + gzip.compress(b"exported NAR")
    monkeypatch.setattr(RECEIVER.sys, "stdin", io.TextIOWrapper(io.BytesIO(wire)))
    real_path = Path
    monkeypatch.setattr(RECEIVER, "Path", lambda value: tmp_path if value == release else real_path(value))
    monkeypatch.setattr(RECEIVER, "STATE", tmp_path / "state")
    monkeypatch.setattr(RECEIVER.subprocess, "check_output", lambda *_a, **_k: "")
    monkeypatch.setattr(RECEIVER.os, "statvfs", lambda _: SimpleNamespace(
        f_bavail=10**12, f_frsize=1))
    imported = []

    class Sink(io.BytesIO):
        def close(self):
            imported.append(self.getvalue())
            super().close()

    class Importer:
        def __init__(self):
            self.stdin = Sink()

        @staticmethod
        def wait():
            return 0

        @staticmethod
        def poll():
            return 0

    calls = []
    monkeypatch.setattr(RECEIVER.subprocess, "Popen", lambda args, **_kw: (
        calls.append(args) or Importer()))

    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(
            {"candidate": str(tmp_path / "candidate")} if args[1] == "machine-bind"
            else {"status": "ready"}) + "\n")

    monkeypatch.setattr(RECEIVER.subprocess, "run", run)
    RECEIVER.receive(action)
    assert imported == [b"exported NAR"]
    assert calls[0] == ["nix-store", "--import"]
    candidate = str(tmp_path / "candidate")
    assert calls[1] == [coordinator, "machine-bind", release, "-"]
    assert calls[2] == [coordinator, "plan", candidate]
    assert calls[3][0] == "nix-store"
    if action == "apply":
        assert calls[4][0] == "systemd-run"
        assert calls[4][-3:] == [coordinator, "apply", candidate]
    else:
        assert len(calls) == 4
