"""Recovery must not reset a healthy display or repeatedly reset a failed one."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'lifecycle/runtime/tools/gpu-recovery.py'
SPEC = importlib.util.spec_from_file_location('gpu_recovery', SOURCE)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    events = []
    def capture(directory, report):
        events.append('capture')
        report.update(boot_id='boot', recent_kernel={'stdout': 'flip_done timed out'})
    def reset(directory, fd):
        events.append('reset')
        return {'returncode': 0}
    monkeypatch.setattr(recovery, 'capture', capture)
    monkeypatch.setattr(recovery, 'reset', reset)
    monkeypatch.setattr(recovery, 'command', lambda *a, **k: {'returncode': 0})
    return tmp_path / 'gpu', tmp_path / 'run', tmp_path / 'logs', events


def test_capture_never_reads_reset_interface(setup):
    gpu, run, logs, events = setup
    recovery.execute('capture', False, gpu, run, logs)
    assert events == ['capture']
    assert not (run / 'reset-attempted.json').exists()


def test_reset_requires_confirmation_and_recent_timeout(setup, monkeypatch):
    gpu, run, logs, events = setup
    with pytest.raises(RuntimeError, match='confirm'):
        recovery.execute('recover', False, gpu, run, logs)
    monkeypatch.setattr(recovery, 'capture', lambda d, r: r.update(recent_kernel={'stdout': ''}))
    with pytest.raises(RuntimeError, match='no recent'):
        recovery.execute('recover', True, gpu, run, logs)
    assert 'reset' not in events


def test_capture_precedes_reset_and_second_attempt_is_refused(setup):
    gpu, run, logs, events = setup
    result = recovery.execute('recover', True, gpu, run, logs)
    assert events == ['capture', 'reset']
    assert result['physical_recovery_verified'] is False
    with pytest.raises(FileExistsError):
        recovery.execute('recover', True, gpu, run, logs)
    assert events.count('reset') == 1


@pytest.mark.parametrize('reset_result', [{'timeout': True, 'pid': 42}, {'returncode': 1}])
def test_failed_reset_blocks_desktop_restart(setup, monkeypatch, reset_result):
    gpu, run, logs, events = setup
    monkeypatch.setattr(recovery, 'reset', lambda *a: reset_result)
    with pytest.raises(RuntimeError, match='reset failed'):
        recovery.execute('recover', True, gpu, run, logs)
    with pytest.raises(RuntimeError, match='did not finish'):
        recovery.execute('restart-desktop', True, gpu, run, logs)
    marker = json.loads((run / 'reset-attempted.json').read_text())
    report = json.loads(Path(marker['report']).read_text())
    assert report['physical_recovery_verified'] is False
    assert report['reset'] == reset_result


def test_desktop_restart_is_separate_and_only_once(setup):
    gpu, run, logs, events = setup
    recovery.execute('recover', True, gpu, run, logs)
    assert not (run / 'desktop-restart-attempted').exists()
    recovery.execute('restart-desktop', True, gpu, run, logs)
    with pytest.raises(FileExistsError):
        recovery.execute('restart-desktop', True, gpu, run, logs)


def test_wrong_or_ambiguous_device_is_refused(tmp_path):
    devices = tmp_path / 'bus/pci/devices'
    devices.mkdir(parents=True)
    with pytest.raises(RuntimeError, match='exactly one'):
        recovery.device(tmp_path)
    for name in ('0000:00:10.0', '0000:00:11.0'):
        gpu = devices / name
        gpu.mkdir()
        (gpu / 'vendor').write_text('0x1002')
        (gpu / 'device').write_text('0x1681')
        (gpu / 'driver').symlink_to(tmp_path / 'drivers/amdgpu')
    with pytest.raises(RuntimeError, match='exactly one'):
        recovery.device(tmp_path)


def test_reset_timeout_does_not_wait_again_for_blocked_kernel_task(monkeypatch):
    class Child:
        pid = 42
        calls = 0
        killed = False
        def wait(self, timeout):
            self.calls += 1
            raise subprocess.TimeoutExpired('cat', timeout)
        def kill(self):
            self.killed = True
    child = Child()
    monkeypatch.setattr(recovery.subprocess, 'Popen', lambda *a, **k: child)
    assert recovery.reset(Path('/gpu'), 9)['timeout']
    assert child.calls == 1 and child.killed
