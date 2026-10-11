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


@pytest.mark.parametrize("action", ["prepare", "plan", "apply"])
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
    installed = "/installed/bin/airlock-machine-deploy"
    monkeypatch.setattr(RECEIVER, "ENGINE", installed)
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
        result = ({"candidate": str(tmp_path / "candidate")} if args[1] == "machine-bind"
                  else {"plan": {"status": "ready"}, "state": "stable"} if args[1] == "prepared-check"
                  else {"status": "ready"})
        return SimpleNamespace(stdout=json.dumps(result) + "\n")

    monkeypatch.setattr(RECEIVER.subprocess, "run", run)
    RECEIVER.receive(action)
    assert imported == [b"exported NAR"]
    assert calls[0] == ["nix-store", "--import"]
    if action == "prepare":
        assert calls[1] == [installed, "admission-prepare", release, "--caller", "sys-ctrl"]
        assert len(calls) == 2
        return
    candidate = str(tmp_path / "candidate")
    assert calls[1] == [coordinator, "machine-bind", release, "-"]
    assert calls[2] == [coordinator, "plan", candidate]
    assert calls[3][0] == "nix-store"
    if action == "apply":
        assert calls[4][0] == "systemd-run"
        assert calls[4][-3:] == [coordinator, "apply", candidate]
    else:
        assert calls[4] == [coordinator, "prepared-check", candidate]
        assert len(calls) == 5


def test_machine_failure_returns_bounded_coordinator_error(monkeypatch, capsys):
    import subprocess

    def fail(_action):
        raise subprocess.CalledProcessError(1, ["coordinator", "machine-bind"],
                                            stderr="specific admission failure")

    monkeypatch.setattr(RECEIVER, "receive", fail)
    monkeypatch.setattr(RECEIVER.sys, "argv", ["receiver", "plan"])
    assert RECEIVER.main() == 1
    reply = json.loads(capsys.readouterr().out)
    assert reply["schema"] == RECEIVER.SCHEMA
    assert "specific admission failure" in reply["error"]
    assert len(reply["error"]) <= 4096


@pytest.mark.parametrize('change', ['none', 'state', 'plan', 'identity', 'tamper', 'pruned'])
def test_prepared_apply_checks_state_without_import_or_binding(monkeypatch, tmp_path, capsys, change):
    monkeypatch.setattr(RECEIVER, 'STATE', tmp_path)
    checked = {'plan': {'changed': ['sys-agent']}, 'state': 'before'}
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if change == 'pruned' and len(calls) > 1 and args[1] == 'prepared-check':
            raise RECEIVER.subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=json.dumps(checked))
    monkeypatch.setattr(RECEIVER.subprocess, 'run', run)
    monkeypatch.setattr(RECEIVER.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('import started'))
    token, saved = RECEIVER.prepared_record('/candidate', '/coordinator', '/release', 'r', 'l')
    header = {'prepared': token, 'release': '/release', 'revision': 'r', 'lock_hash': 'l'}
    if change == 'state':
        checked['state'] = 'after'
    if change == 'plan':
        checked['plan'] = {'changed': ['sys-gui']}
    if change == 'identity':
        header['revision'] = 'other'
    if change == 'tamper':
        saved['coordinator'] = '/malicious'
        (tmp_path / 'prepared' / (token + '.json')).write_text(json.dumps(saved))
    if change == 'none':
        RECEIVER.apply_prepared(header)
        result = json.loads(capsys.readouterr().out)
        assert result['prepared_reused'] is True
        assert result['paths_transferred'] == 0
        assert calls[-1][-2:] == ['--expected-state', 'before']
    else:
        with pytest.raises((ValueError, RECEIVER.subprocess.CalledProcessError)):
            RECEIVER.apply_prepared(header)
        assert not any(call[0] == 'systemd-run' for call in calls)
    assert not any('machine-bind' in call for call in calls)


def test_prepared_record_flushes_file_before_replace_and_directory_after(monkeypatch, tmp_path):
    import os
    import stat

    monkeypatch.setattr(RECEIVER, "STATE", tmp_path)
    monkeypatch.setattr(RECEIVER.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(stdout=json.dumps({"plan": {}, "state": "stable"})))
    events = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(RECEIVER.os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    token, payload = RECEIVER.prepared_record("candidate", "coordinator", "release", "revision", "lock")
    assert events == ["file", "replace", "directory"]
    assert json.loads((tmp_path / "prepared" / (token + ".json")).read_text()) == payload
    assert not (tmp_path / "prepared" / (token + ".new")).exists()
