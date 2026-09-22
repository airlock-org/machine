from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
from io import BytesIO

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_dev_machine_deploy",
    ROOT / "lifecycle/runtime/tools/sys-dev-machine-deploy.py",
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


def release_manifest(revision="a" * 40, lock_hash="b" * 64):
    return {
        "schema": "airlock.machine-release.v1",
        "revision": revision,
        "lockHash": lock_hash,
        "profile": "usb-lab",
        "coordinator": "/nix/store/coordinator/bin/airlock-machine-deploy",
    }


def test_dom0_bridge_allocates_the_terminal_required_by_qrexec(monkeypatch):
    class Process:
        stdin = BytesIO()
        stdout = BytesIO()

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout):
            return 0

    calls = []

    def popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return Process()

    monkeypatch.setattr(DEPLOY.subprocess, "Popen", popen)
    monkeypatch.setattr(
        DEPLOY.Dom0,
        "until",
        lambda _self, tokens, timeout: (tokens[-1], b""),
    )

    DEPLOY.Dom0()

    assert calls[0][0] == [
        "script",
        "--quiet",
        "--return",
        "--flush",
        "--command",
        "airlock-dom0-shell",
        "/dev/null",
    ]


def test_source_identity_requires_a_clean_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "flake.lock").write_text("{}")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "add", "flake.lock"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )

    revision, lock_hash = DEPLOY.source_identity(repo)
    assert len(revision) == 40
    assert len(lock_hash) == 64

    (repo / "flake.lock").write_text("changed")
    with pytest.raises(RuntimeError, match="commit or stash"):
        DEPLOY.source_identity(repo)


def test_build_pins_release_and_checks_committed_provenance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "flake.lock").write_text("{}")
    state = tmp_path / "state"
    store = tmp_path / "store-root"
    release = "/nix/store/release-airlock-machine-release"
    physical_release = store / release.removeprefix("/")
    physical_release.mkdir(parents=True)
    manifest = release_manifest()
    (physical_release / "release.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("AIRLOCK_CONFIG_NIX_STORE_ROOT", str(store))
    monkeypatch.setattr(
        DEPLOY,
        "source_identity",
        lambda _repo: (manifest["revision"], manifest["lockHash"]),
    )
    calls = []

    def fake_command(*args, **_kwargs):
        calls.append(args)
        if "--json" in args:
            return json.dumps([{"outputs": {"out": release}}])
        return ""

    monkeypatch.setattr(DEPLOY, "command", fake_command)
    receipt = DEPLOY.build("usb-lab", repo, state)

    assert receipt == {
        "schema": "airlock.sys-dev-machine-release.v1",
        "revision": manifest["revision"],
        "lock_hash": manifest["lockHash"],
        "profile": "usb-lab",
        "release": release,
    }
    assert (
        json.loads((state / manifest["revision"] / "build.json").read_text()) == receipt
    )
    assert calls[-1] == (
        "airlock-config-nix",
        "build",
        release,
        "--out-link",
        str(state / manifest["revision"] / "release"),
    )


def test_build_rejects_release_with_different_revision(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "flake.lock").write_text("{}")
    store = tmp_path / "store-root"
    release = "/nix/store/release-airlock-machine-release"
    physical_release = store / release.removeprefix("/")
    physical_release.mkdir(parents=True)
    manifest = release_manifest(revision="c" * 40)
    (physical_release / "release.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("AIRLOCK_CONFIG_NIX_STORE_ROOT", str(store))
    monkeypatch.setattr(DEPLOY, "source_identity", lambda _repo: ("a" * 40, "b" * 64))
    monkeypatch.setattr(
        DEPLOY,
        "command",
        lambda *_args, **_kwargs: json.dumps([{"outputs": {"out": release}}]),
    )

    with pytest.raises(RuntimeError, match="provenance"):
        DEPLOY.build("usb-lab", repo, tmp_path / "state")


class TransferSession:
    def __init__(self, existing, free=100 * 1024**3):
        self.existing = existing
        self.free = free
        self.commands = []

    def run(self, script, timeout=60):
        self.commands.append((script, timeout))
        if script == DEPLOY.REMOTE_INVENTORY:
            return "\n".join(self.existing)
        if "statvfs" in script:
            return str(self.free)
        return ""


def test_transfer_exports_only_paths_missing_from_dom0(tmp_path, monkeypatch):
    base = "/nix/store/base"
    added = "/nix/store/added"
    monkeypatch.setattr(
        DEPLOY,
        "closure_metadata",
        lambda _release: {
            base: {"path": base, "narSize": 10},
            added: {"path": added, "narSize": 20},
        },
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    arguments = tmp_path / "export-arguments"
    exporter = binaries / "airlock-config-nix-store"
    exporter.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$EXPORT_ARGUMENTS"\nprintf payload\n'
    )
    exporter.chmod(0o755)
    monkeypatch.setenv("EXPORT_ARGUMENTS", str(arguments))
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    session = TransferSession([base])

    result = DEPLOY.transfer("/nix/store/release", session, tmp_path / "state")

    assert result == {"paths_total": 2, "paths_transferred": 1, "nar_bytes": 20}
    assert arguments.read_text().splitlines() == ["--export", added]
    transfer_command, timeout = session.commands[-1]
    assert "qrexec-client -d sys-dev" in transfer_command
    assert "gzip -dc" in transfer_command
    assert "nix-store --import" in transfer_command
    assert timeout == 3600
    assert list((tmp_path / "state" / "incoming").iterdir()) == []


def test_transfer_requires_only_import_space_and_reserve(tmp_path, monkeypatch):
    added = "/nix/store/added"
    nar_size = 1024
    monkeypatch.setattr(
        DEPLOY,
        "closure_metadata",
        lambda _release: {added: {"path": added, "narSize": nar_size}},
    )
    session = TransferSession([], free=2 * 1024**3 + nar_size - 1)

    with pytest.raises(RuntimeError, match="free to import this release"):
        DEPLOY.transfer("/nix/store/release", session, tmp_path / "state")

    assert not (tmp_path / "state").exists()


class PlanSession:
    instances = []

    def __init__(self):
        self.commands = []
        self.closed = False
        self.__class__.instances.append(self)

    def run(self, script, timeout=60):
        self.commands.append((script, timeout))
        if script.startswith("cat "):
            return json.dumps(release_manifest())
        if " plan " in f" {script} ":
            return json.dumps({"status": "planned", "activated": False})
        raise AssertionError(f"unexpected dom0 command: {script}")

    def close(self):
        self.closed = True


def test_plan_builds_transfers_and_never_starts_apply(tmp_path, monkeypatch, capsys):
    receipt = {
        "revision": "a" * 40,
        "lock_hash": "b" * 64,
        "profile": "usb-lab",
        "release": "/nix/store/release-airlock-machine-release",
    }
    monkeypatch.setattr(DEPLOY, "build", lambda *_args: receipt)
    monkeypatch.setattr(DEPLOY, "transfer", lambda *_args: None)
    monkeypatch.setattr(DEPLOY, "Dom0", PlanSession)
    PlanSession.instances.clear()

    assert DEPLOY.main(["plan", "--state", str(tmp_path)]) == 0

    session = PlanSession.instances[0]
    assert session.closed is True
    assert any(" plan " in f" {script} " for script, _timeout in session.commands)
    assert all("systemd-run" not in script for script, _timeout in session.commands)
    assert '"activated": false' in capsys.readouterr().out


def test_apply_uses_release_coordinator_in_a_detached_worker(tmp_path, monkeypatch):
    receipt = {
        "revision": "a" * 40,
        "lock_hash": "b" * 64,
        "profile": "usb-lab",
        "release": "/nix/store/release-airlock-machine-release",
    }
    started = []
    monkeypatch.setattr(DEPLOY, "build", lambda *_args: receipt)
    monkeypatch.setattr(DEPLOY, "transfer", lambda *_args: None)
    monkeypatch.setattr(DEPLOY, "Dom0", PlanSession)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(
        DEPLOY,
        "run_detached",
        lambda _session, action: started.append(action) or "test-unit",
    )
    PlanSession.instances.clear()

    assert DEPLOY.main(["apply", "--state", str(tmp_path)]) == 0
    assert started == [
        [
            "/nix/store/coordinator/bin/airlock-machine-deploy",
            "apply",
            receipt["release"],
        ]
    ]
