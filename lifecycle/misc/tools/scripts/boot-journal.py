"""Persist the initramfs journal as it arrives, including failed activation."""
import os
from pathlib import Path
import subprocess
import sys


def capture(journalctl, root=Path('/sysroot')):
    directory = root / 'var/log/airlock-boot'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    path = directory / f'{boot_id}-initrd.log'
    print(f'Airlock: saving early boot journal to {path}', flush=True)
    with path.open('ab', buffering=0) as output:
        os.chmod(path, 0o600)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with subprocess.Popen([journalctl, '--boot', '--follow', '--no-pager',
                               '--output=short-monotonic'], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            for line in process.stdout:
                output.write(line)
                os.fsync(output.fileno())
            if process.wait():
                raise RuntimeError('early boot journal recorder exited unsuccessfully')


if __name__ == '__main__':
    capture(sys.argv[1])
