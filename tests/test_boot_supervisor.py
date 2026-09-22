"""Boot supervision must preserve evidence and refuse false readiness."""

import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "lifecycle/runtime/tools/boot-supervisor.py"
START_READY = ROOT / "lifecycle/misc/tools/scripts/start-qube-ready.sh"


def executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def supervisor_env(tmp_path):
    boot_id = tmp_path / "boot-id"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    runtime = tmp_path / "run"
    diagnostic = tmp_path / "root"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for command in ("xl", "qvm-ls", "systemctl", "free", "journalctl"):
        executable(
            binaries / command,
            "#!/bin/sh\nprintf '%s\\n' \"$0 $*\"\n",
        )
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "AIRLOCK_BOOT_ID_FILE": str(boot_id),
        "AIRLOCK_BOOT_STATE_ROOT": str(state),
        "AIRLOCK_BOOT_LOG_ROOT": str(logs),
        "AIRLOCK_BOOT_RUN_ROOT": str(runtime),
        "AIRLOCK_BOOT_DIAGNOSTIC_ROOT": str(diagnostic),
    }
    return env, boot_id, state, logs, runtime, diagnostic


def run_supervisor(env, *args):
    return subprocess.run(
        ["python3", str(SUPERVISOR), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_interrupted_boot_is_recovered_before_a_new_attempt(tmp_path):
    env, boot_id, state, logs, runtime, diagnostic = supervisor_env(tmp_path)
    console = diagnostic / "var/log/xen/console/guest-sys-gui.log"
    console.parent.mkdir(parents=True)
    console.write_text("last guest output\n", encoding="utf-8")

    boot_id.write_text("first-boot\n", encoding="utf-8")
    run_supervisor(env, "prepare")
    run_supervisor(env, "stage", "gui", "waiting for sys-gui")
    assert json.loads((state / "in-progress.json").read_text())["stage"] == "gui"

    boot_id.write_text("second-boot\n", encoding="utf-8")
    run_supervisor(env, "prepare")
    recovered = json.loads((state / "first-boot/state.json").read_text())
    assert recovered["status"] == "interrupted"
    assert recovered["recovered_by"] == "second-boot"
    assert (runtime / "recovery-required.json").exists()
    copies = list(
        (logs / "first-boot/snapshots").glob(
            "recovered-on-*/var/log/xen/console/guest-sys-gui.log"
        )
    )
    assert len(copies) == 1
    assert copies[0].read_text() == "last guest output\n"
    journal = next((logs / "first-boot/snapshots").glob("recovered-on-*/journal.txt"))
    assert "--boot first-boot" in journal.read_text()
    assert not (state / "in-progress.json").exists()

    run_supervisor(env, "recovery-ack")
    recovery = json.loads((state / "second-boot/state.json").read_text())
    assert recovery["status"] == "complete"


def test_completion_is_the_only_normal_path_that_clears_marker(tmp_path):
    env, boot_id, state, _logs, _runtime, _diagnostic = supervisor_env(tmp_path)
    boot_id.write_text("normal-boot\n", encoding="utf-8")
    run_supervisor(env, "prepare")
    run_supervisor(env, "stage", "network", "waiting")
    assert (state / "in-progress.json").exists()
    run_supervisor(env, "complete", "all guests ready")
    assert not (state / "in-progress.json").exists()
    result = json.loads((state / "normal-boot/state.json").read_text())
    assert result["stage"] == "ready"
    assert result["status"] == "complete"


def test_qube_start_requires_qrexec_and_exact_generation(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    topology = tmp_path / "topology.json"
    topology.write_text(
        json.dumps(
            {
                "domains": {
                    "sys-dev": {
                        "backend": {"system": {"source": "/nix/store/expected-system"}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("qvm-start", "qvm-check", "qvm-ls"):
        executable(
            binaries / name,
            f'#!/bin/sh\nprintf \'{name} %s\\n\' "$*" >>"$CALLS"\n',
        )
    executable(
        binaries / "qrexec-client",
        "#!/bin/sh\nprintf 'qrexec-client %s\\n' \"$*\" >>\"$CALLS\"\nprintf '/nix/store/expected-system\\n'\n",
    )
    executable(
        binaries / "supervisor",
        '#!/bin/sh\nprintf \'supervisor %s\\n\' "$*" >>"$CALLS"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "AIRLOCK_QVM_START": str(binaries / "qvm-start"),
        "AIRLOCK_QVM_CHECK": str(binaries / "qvm-check"),
        "AIRLOCK_QVM_LS": str(binaries / "qvm-ls"),
        "AIRLOCK_QREXEC_CLIENT": str(binaries / "qrexec-client"),
        "AIRLOCK_BOOT_SUPERVISOR": str(binaries / "supervisor"),
        "AIRLOCK_BOOT_ATTEMPT_LOG_DIR": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        ["bash", str(START_READY), "sys-dev", str(topology)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    trace = calls.read_text()
    assert "qvm-start --skip-if-running -- sys-dev" in trace
    assert "qrexec-client -d sys-dev user:readlink -f /run/current-system" in trace
    assert "sys-dev is qrexec-ready on /nix/store/expected-system" in trace


def test_wrong_guest_generation_fails_and_is_recorded(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    topology = tmp_path / "topology.json"
    topology.write_text(
        '{"domains":{"sys-gui":{"backend":{"system":{"source":"/nix/store/expected"}}}}}',
        encoding="utf-8",
    )
    for name in ("qvm-start", "qvm-check", "qvm-ls"):
        executable(binaries / name, "#!/bin/sh\nexit 0\n")
    executable(binaries / "qrexec-client", "#!/bin/sh\necho /nix/store/wrong\n")
    executable(
        binaries / "supervisor",
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$CALLS"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "AIRLOCK_QVM_START": str(binaries / "qvm-start"),
        "AIRLOCK_QVM_CHECK": str(binaries / "qvm-check"),
        "AIRLOCK_QVM_LS": str(binaries / "qvm-ls"),
        "AIRLOCK_QREXEC_CLIENT": str(binaries / "qrexec-client"),
        "AIRLOCK_BOOT_SUPERVISOR": str(binaries / "supervisor"),
        "AIRLOCK_BOOT_ATTEMPT_LOG_DIR": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        ["bash", str(START_READY), "sys-gui", str(topology)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "fail qube:sys-gui" in calls.read_text()
