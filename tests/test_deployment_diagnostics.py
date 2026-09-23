"""Deployment evidence is durable before command completion and survives errors."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(path):
    spec = importlib.util.spec_from_file_location(
        "diagnostic_test_subject", ROOT / path
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


deploy = module("lifecycle/runtime/tools/airlock_machine_deploy/diagnostics.py")


def events(diag):
    return [
        json.loads(line)
        for line in (diag.directory / "events.jsonl").read_text().splitlines()
    ]


def test_command_output_and_exit(tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    assert (
        diag.execute(
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        )
        == "out"
    )
    assert (diag.directory / "command-0001.stderr").read_text() == "err\n"
    assert [event["event"] for event in events(diag)] == [
        "command-start",
        "command-finish",
    ]
    assert events(diag)[-1]["returncode"] == 0


def test_command_failure_preserves_stderr(tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    with pytest.raises(subprocess.CalledProcessError):
        diag.execute(
            sys.executable,
            "-c",
            "import sys; print('failure', file=sys.stderr); sys.exit(7)",
        )
    assert "failure" in (diag.directory / "command-0001.stderr").read_text()
    assert events(diag)[-1]["event"] == "command-error"


def test_timeout_preserves_partial_output(tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    with pytest.raises(subprocess.TimeoutExpired):
        diag.execute(
            sys.executable,
            "-u",
            "-c",
            "import time; print('starting'); time.sleep(30)",
            timeout=0.1,
        )
    assert "starting" in (diag.directory / "command-0001.stdout").read_text()
    assert events(diag)[-1]["timeout"] is True


def test_output_visible_while_command_running(tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    worker = threading.Thread(
        target=lambda: diag.execute(
            sys.executable, "-u", "-c", "import time; print('live'); time.sleep(1)"
        )
    )
    worker.start()
    try:
        path = diag.directory / "command-0001.stdout"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists() and "live" in path.read_text():
                break
            time.sleep(0.01)
        assert worker.is_alive()
        assert "live" in path.read_text()
    finally:
        worker.join(timeout=5)


def test_capture_failure_is_observational(tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    diag.capture("missing", ["/definitely-not-a-real-command"])
    assert events(diag)[-1]["event"] == "capture-error"


def test_journal_cursor_prevents_recopying(monkeypatch, tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    calls = []

    def collect(args, **kwargs):
        calls.append(args)
        kwargs["stdout"].write(b"evidence\n-- cursor: test-cursor\n")

    monkeypatch.setattr(deploy.subprocess, "run", collect)
    diag.journal_snapshot()
    diag.journal_snapshot()
    assert "--after-cursor" not in calls[0]
    assert calls[1][-2:] == ["--after-cursor", "test-cursor"]
    assert (diag.directory / "journal.log").read_text().count("evidence") == 2


def test_observer_continues_after_journal_failure(monkeypatch, tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])

    def fail():
        raise OSError("journal unavailable")

    monkeypatch.setattr(diag, "journal_snapshot", fail)
    monkeypatch.setattr(
        diag, "capture", lambda label, args: diag.save(label, b"ActiveState=active")
    )
    diag.snapshot()
    assert events(diag)[0]["label"] == "journal"
    assert events(diag)[1]["event"] == "services-observed"


def test_collector_stops(monkeypatch, tmp_path):
    diag = deploy.DeploymentDiagnostics(tmp_path, [])
    observed = threading.Event()
    monkeypatch.setattr(diag, "snapshot", observed.set)
    diag.start()
    assert observed.wait(2)
    diag.close()
    assert not diag.thread.is_alive()


def test_qubesd_logging_does_not_pollute_json_stdout(monkeypatch, capsys):
    storage = module("lifecycle/runtime/tools/scripts/nix-domain-storage.py")
    calls = []
    monkeypatch.setattr(
        storage.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    storage.run_systemctl("start")
    output = capsys.readouterr()
    assert not output.out
    records = [json.loads(line) for line in output.err.splitlines()]
    assert [r["event"] for r in records] == [
        "service-command-start",
        "service-command-finish",
    ]
    assert "sockets" in records[-1]
    assert calls == [["systemctl", "start", "qubesd.service"]]
