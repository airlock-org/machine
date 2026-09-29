"""Run upstream boot installation with only locally available Nix profiles.

Retained boot entries are independently sealed on the ESP. A private mount
namespace hides dangling profile links from upstream bootspec discovery without
changing the real profiles, deleting history, or mounting old host stores.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Invoked by absolute filename in an unshared child; retain package imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from airlock_qubes_storage._engine import core, host  # noqa: E402


def copy_profiles(source, target):
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_symlink():
            if path.exists():
                dest = target / path.name
                dest.symlink_to(os.readlink(path))
                st = path.lstat()
                os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        elif path.is_dir() and path.name == 'system-profiles':
            copy_profiles(path, target / path.name)


def install(system):
    core.store_entry(system)
    profiles = Path('/nix/var/nix/profiles')
    with tempfile.TemporaryDirectory(prefix='airlock-boot-profiles-') as directory:
        copied = Path(directory) / 'profiles'
        copy_profiles(profiles, copied)
        if not (copied / 'system').exists():
            core.fail('selected host profile is unavailable for boot installation')
        host.command('mount', '--bind', str(copied), str(profiles))
        try:
            subprocess.run([system + '/bin/switch-to-configuration', 'boot'], check=True)
        finally:
            host.command('umount', str(profiles))


if __name__ == '__main__':
    install(sys.argv[1])
