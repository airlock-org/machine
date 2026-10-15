from __future__ import annotations

import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from io import BytesIO
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "airlock_cli",
    ROOT / "lifecycle/runtime/tools/airlock.py",
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


@pytest.mark.parametrize("arguments", [[], ["status"], ["check"], ["apply"], ["machine"], ["machine", "unknown"]])
def test_invalid_commands_do_not_contact_the_machine(monkeypatch, arguments):
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda *_a, **_k: pytest.fail("unexpected controller call"))
    monkeypatch.setattr(DEPLOY.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected RPC call"))
    with pytest.raises(SystemExit) as error:
        DEPLOY.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [["--help"], ["machine", "--help"]])
def test_help_is_available_without_workspace_or_qrexec(tmp_path, arguments):
    result = subprocess.run(
        [DEPLOY.sys.executable, str(SPEC.origin), *arguments],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "machine" in result.stdout
    assert not result.stderr


def release_manifest(revision="a" * 40, lock_hash="b" * 64):
    return {
        "schema": "airlock.machine-release.v1",
        "revision": revision,
        "lockHash": lock_hash,
        "profile": "usb-lab",
        "coordinator": "/nix/store/coordinator/bin/airlock-machine-deploy",
    }


def test_guest_apply_delegates_to_controller(monkeypatch):
    calls = []
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert DEPLOY.main(["machine", "guest-apply", "--target", "usr-home"]) == 0
    assert calls == [["airlock-sys-ctrl-guest-deploy", "apply", "usr-home",
                      "--repo", "/home/user/airlock-machine"]]


def test_guest_plan_passes_explicit_checkout_to_controller(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert DEPLOY.main(["machine", "guest-plan", "--target", "usr-assistant", "--repo", str(tmp_path)]) == 0
    assert calls == [["airlock-sys-ctrl-guest-deploy", "plan", "usr-assistant",
                      "--repo", str(tmp_path)]]


@pytest.mark.parametrize("action", ["build", "plan", "apply"])
def test_whole_machine_actions_delegate_to_controller_by_default(monkeypatch, action):
    calls = []
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert DEPLOY.main(["machine", action]) == 0
    assert calls == [["airlock-sys-ctrl-guest-deploy", "machine-" + action,
                      "--repo", str(DEPLOY.DEFAULT_REPO)]]


@pytest.mark.parametrize("action", ["build", "plan", "apply"])
def test_rename_submission_uses_frontend_from_the_verified_checkout(tmp_path, monkeypatch, action):
    events = []
    monkeypatch.setattr(DEPLOY, "source_identity", lambda repo: events.append(("verify", repo)))
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda argv: events.append(argv) or 0)
    assert DEPLOY.main(["machine", action, "--assistant-rename", "--repo", str(tmp_path)]) == 0
    assert events == [("verify", tmp_path), [DEPLOY.sys.executable,
        str(tmp_path / "lifecycle/runtime/tools/sys-ctrl-guest-deploy.py"),
        "machine-" + action, "--repo", str(tmp_path), "--assistant-rename"]]


@pytest.mark.parametrize("arguments", [["status", "--assistant-rename"], ["plan", "--assistant-rename", "--legacy-dom0"]])
def test_rename_rejects_incompatible_actions_before_submission(monkeypatch, arguments):
    monkeypatch.setattr(DEPLOY.subprocess, "call", lambda *_: pytest.fail("submission attempted"))
    with pytest.raises(SystemExit) as error:
        DEPLOY.main(["machine", *arguments])
    assert error.value.code == 2


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
        "schema": "airlock.sys-agent-machine-release.v1",
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


@pytest.mark.timeout(10)
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

    class ReadingTransferSession(TransferSession):
        def run(self, script, timeout=60):
            if "qrexec-client -d usr-assistant" in script:
                self.commands.append((script, timeout))
                # Reproduce a fast, one-path export that finishes before qrexec
                # starts opening the FIFO. The writer must wait for this reader.
                time.sleep(0.1)
                fifo = next((tmp_path / "state" / "incoming").iterdir())
                with fifo.open("rb") as stream:
                    self.payload = gzip.decompress(stream.read())
                return ""
            return super().run(script, timeout)

    session = ReadingTransferSession([base])

    result = DEPLOY.transfer("/nix/store/release", session, tmp_path / "state")

    assert result == {"paths_total": 2, "paths_transferred": 1, "nar_bytes": 20}
    assert arguments.read_text().splitlines() == ["--export", added]
    assert session.payload == b"payload"
    transfer_command, timeout = session.commands[-1]
    assert "qrexec-client -d usr-assistant" in transfer_command
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

    assert DEPLOY.main(["machine", "plan", "--legacy-dom0", "--state", str(tmp_path)]) == 0

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

    assert DEPLOY.main(["machine", "apply", "--legacy-dom0", "--state", str(tmp_path)]) == 0
    assert started == [
        [
            "/nix/store/coordinator/bin/airlock-machine-deploy",
            "apply",
            receipt["release"],
        ]
    ]


@pytest.mark.parametrize(
    "action,arguments",
    [
        (
            "release-prune",
            [
                "--release",
                "/nix/store/old-release",
                "--confirm",
                "/nix/store/old-release",
            ],
        ),
        (
            "trash-purge",
            ["--identity", "123-disposable", "--confirm", "123-disposable"],
        ),
    ],
)
def test_maintenance_is_detached_and_does_not_build(action, arguments, monkeypatch):
    started = []
    monkeypatch.setattr(DEPLOY, "Dom0", PlanSession)
    monkeypatch.setattr(DEPLOY, "deployed_engine", lambda _session: "/controller")
    monkeypatch.setattr(
        DEPLOY, "build", lambda *_args: pytest.fail("maintenance must not build")
    )
    monkeypatch.setattr(
        DEPLOY,
        "run_detached",
        lambda _session, command: started.append(command) or "unit",
    )
    assert DEPLOY.main(["machine", action, "--legacy-dom0", *arguments]) == 0
    assert started == [["/controller", action, arguments[1], "--confirm", arguments[3]]]


@pytest.mark.parametrize(
    "action,arguments,expected",
    [
        (
            "release-prune",
            [
                "--release", "/nix/store/old-release",
                "--confirm", "/nix/store/old-release",
                "--allow-unexpired-trash", "123-disposable",
            ],
            [
                "/controller", "release-prune", "/nix/store/old-release",
                "--confirm", "/nix/store/old-release",
                "--allow-unexpired-trash", "123-disposable",
            ],
        ),
        (
            "trash-purge",
            [
                "--identity", "123-disposable",
                "--confirm", "123-disposable",
                "--before-expiry",
            ],
            [
                "/controller", "trash-purge", "123-disposable",
                "--confirm", "123-disposable", "--before-expiry",
            ],
        ),
    ],
)
def test_maintenance_forwards_early_purge_override(
    action, arguments, expected, monkeypatch
):
    started = []
    monkeypatch.setattr(DEPLOY, "Dom0", PlanSession)
    monkeypatch.setattr(DEPLOY, "deployed_engine", lambda _session: "/controller")
    monkeypatch.setattr(
        DEPLOY, "run_detached",
        lambda _session, command: started.append(command) or "unit",
    )
    assert DEPLOY.main(["machine", action, "--legacy-dom0", *arguments]) == 0
    assert started == [expected]


def test_maintenance_refuses_wrong_confirmation_before_connecting(monkeypatch):
    monkeypatch.setattr(DEPLOY, "Dom0", lambda: pytest.fail("unexpected connection"))
    with pytest.raises(SystemExit):
        DEPLOY.main(["machine", "trash-purge", "--identity", "123-vm", "--confirm", "456-vm"])


@pytest.mark.parametrize("action", ["status", "verify", "resume", "rollback"])
def test_normal_recovery_uses_typed_dom0_route_without_controller(monkeypatch, action):
    monkeypatch.setattr(DEPLOY, "Dom0", lambda: pytest.fail("normal recovery opened the development shell"))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, json.loads(kwargs["input"])))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(DEPLOY.subprocess, "run", run)
    assert DEPLOY.main(["machine", action]) == 0
    assert calls == [(["qrexec-client-vm", "dom0", "airlock.Ctrl.MachineControl"],
                      {"schema": "airlock.machine-control.v1", "action": action})]
