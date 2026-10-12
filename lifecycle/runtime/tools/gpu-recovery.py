"""Manual sys-gui display recovery. No PCI reassignment or automatic retries."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


DISPLAY_TIMEOUT = re.compile(r"flip_done timed out|flip_done or hw_done timed out|hw_done or flip_done timed out")


def command(args, timeout=10):
    # Files avoid a blocking communicate()/wait() after killing a task stuck
    # in the driver. Bound the evidence size as well as the client wait.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(args, stdout=output, stderr=errors)
        try:
            result = {'argv': args, 'returncode': child.wait(timeout=timeout)}
        except subprocess.TimeoutExpired:
            child.kill()
            result = {'argv': args, 'timeout': True, 'pid': child.pid}
        output.seek(0)
        errors.seek(0)
        result.update(stdout=output.read(1024 * 1024).decode(errors='replace'),
                      stderr=errors.read(1024 * 1024).decode(errors='replace'))
        return result


def device(sysfs=Path('/sys'), debug=Path('/sys/kernel/debug/dri')):
    matches = []
    for path in (sysfs / 'bus/pci/devices').glob('*'):
        if ((path / 'vendor').read_text().strip() == '0x1002'
                and (path / 'device').read_text().strip() == '0x1681'
                and (path / 'driver').resolve().name == 'amdgpu'):
            matches.append(path)
    if len(matches) != 1:
        raise RuntimeError('require exactly one amdgpu-owned lab Radeon 680M')
    directory = debug / matches[0].name
    if not (directory / 'amdgpu_gpu_recover').exists():
        raise RuntimeError('exact GPU recovery interface is unavailable')
    return directory


def capture(directory, report):
    """Read only reviewed observational nodes, never glob-read debugfs."""
    report['kernel'] = command(['journalctl', '-k', '-b', '-n', '200', '--no-pager'])
    report['recent_kernel'] = command(['journalctl', '-k', '-b', '--since', '5 minutes ago',
                                       '-n', '200', '--no-pager'])
    report['desktop'] = command(['journalctl', '-b', '-u', 'display-manager', '-n', '80', '--no-pager'])
    report['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    report['kernel_release'] = os.uname().release
    report['cmdline'] = Path('/proc/cmdline').read_text().strip()
    report['gpu'] = directory.name
    report['fences'] = command(['cat', str(directory / 'amdgpu_fence_info')])
    report['firmware'] = command(['cat', str(directory / 'amdgpu_firmware_info')])
    report['psr'] = {}
    for connector in directory.glob('eDP-*'):
        for name in ('psr_capability', 'psr_state', 'disallow_edp_enter_psr'):
            path = connector / name
            if path.exists():
                report['psr'][str(path)] = command(['cat', str(path)])


def reset(directory, lock_fd):
    # cat inherits the lock, including while stuck in an uninterruptible kernel
    # call. A timeout does NOT establish that recovery stopped or completed.
    child = subprocess.Popen(['cat', str(directory / 'amdgpu_gpu_recover')],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             pass_fds=(lock_fd,))
    try:
        return {"returncode": child.wait(timeout=25), "pid": child.pid}
    except subprocess.TimeoutExpired:
        child.kill()
        return {"timeout": True, "pid": child.pid,
                "warning": "kernel recovery may still be running; do not retry or escalate"}


def save(path, report):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(path)


def execute(action, confirmed, directory, runtime, logs):
    if action != 'capture' and not confirmed:
        raise RuntimeError('recovery requires --confirm-display-stall')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = logs / (str(time.time_ns()) + '.json')
        report = {'action': action, 'started_at': time.time(), 'status': 'capturing',
                  'physical_recovery_verified': False}
        capture(directory, report)
        save(path, report)
        if action == 'recover':
            if not DISPLAY_TIMEOUT.search(report['recent_kernel'].get('stdout', '')):
                raise RuntimeError('no recent display flip timeout; reset refused')
            marker = runtime / 'reset-attempted.json'
            # O_EXCL enforces one attempted reset per guest boot, even if a
            # successful reset fails to repair the display's pending flip.
            with marker.open('x') as stream:
                json.dump({'report': str(path), 'boot_id': report['boot_id']}, stream)
            report['status'] = 'reset-requested'
            save(path, report)
            result = reset(directory, lock.fileno())
            report['reset'] = result
            report['status'] = 'unknown' if result.get('timeout') else 'reset-returned'
            report['physical_recovery_verified'] = False
            report['after_kernel'] = command(['journalctl', '-k', '-b', '-n', '80', '--no-pager'])
            save(path, report)
            if result.get('timeout') or result.get('returncode') != 0:
                raise RuntimeError('reset failed or timed out; inspect ' + str(path))
        elif action == 'restart-desktop':
            # Never restart the desktop while a prior reset is still pending.
            marker = runtime / 'reset-attempted.json'
            if not marker.exists():
                raise RuntimeError('capture and attempt GPU recovery before desktop restart')
            previous = json.loads(Path(json.loads(marker.read_text())['report']).read_text())
            if previous.get('reset', {}).get('returncode') != 0:
                raise RuntimeError('previous reset did not finish successfully; desktop restart refused')
            with (runtime / 'desktop-restart-attempted').open('x'):
                pass
            report['restart'] = command(['systemctl', 'restart', 'display-manager'], timeout=30)
            report['status'] = 'desktop-restart-returned'
            save(path, report)
            if report['restart'].get('returncode') != 0:
                raise RuntimeError('desktop restart failed or timed out; inspect ' + str(path))
        else:
            report['status'] = 'captured'
            save(path, report)
        return {'report': str(path), 'status': report['status'],
                'physical_recovery_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'recover', 'restart-desktop'))
    parser.add_argument('--confirm-display-stall', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root in sys-gui using the existing administrative route')
    os.umask(0o077)
    try:
        result = execute(args.action, args.confirm_display_stall, device(),
                         Path('/run/airlock/gpu-recovery'), Path('/var/log/airlock/gpu-recovery'))
    except (OSError, RuntimeError) as error:
        parser.exit(1, 'airlock-gpu-recovery: ' + str(error) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
