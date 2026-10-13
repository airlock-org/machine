"""Run upstream boot installation with only locally available Nix profiles.

Retained boot entries are independently sealed on the ESP. A private mount
namespace hides dangling profile links from upstream bootspec discovery without
changing the real profiles, deleting history, or mounting old host stores.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Invoked by absolute filename in an unshared child; retain package imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from airlock_qubes_storage._engine import core, host  # noqa: E402
from airlock_qubes_storage import host_artifacts  # noqa: E402


def copy_profiles(source, target, *, sealed=(), selected=None):
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_symlink():
            if path.exists():
                if (path.name != 'system' and str(path.resolve()) in sealed
                        and path.resolve() != selected):
                    continue
                dest = target / path.name
                dest.symlink_to(os.readlink(path))
                st = path.lstat()
                os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        elif path.is_dir() and path.name == 'system-profiles':
            copy_profiles(path, target / path.name, sealed=sealed, selected=selected)


def verified_sealed_systems(root, esp):
    systems = set()
    root = Path(root)
    for path in (root / 'host/boots').glob('*.json'):
        record = core.read_json(path)
        if record.get('retired') or not (root / 'host/artifacts' / path.name).exists():
            continue
        host_artifacts.verify(root, esp, record)
        systems.add(record['system'])
    return systems


def publish_loader(staged, loader):
    """Publish prepared entries while preserving the current boot choice."""
    staged, loader = Path(staged), Path(loader)
    previous = (loader / 'loader.conf').read_text().splitlines()
    choices = [line for line in previous if line.split() and line.split()[0] in ('default', 'preferred')]
    if not any(line.split()[0] == 'default' for line in choices):
        core.fail('boot preparation requires an existing default')
    protected = {line.split()[1].removesuffix('.conf') for line in choices}
    entries = loader / 'entries'
    for path in (staged / 'entries').glob('*.conf'):
        core.atomic_write(entries / path.name, path.read_text())
    for path in entries.glob('*.conf'):
        if (not (staged / 'entries' / path.name).exists()
                and not path.name.startswith('airlock-') and path.stem not in protected):
            path.unlink()
    core.fsync_dir(entries)
    lines = [line for line in (staged / 'loader.conf').read_text().splitlines()
             if not line.split() or line.split()[0] not in ('default', 'preferred')]
    core.atomic_write(loader / 'loader.conf', '\n'.join([*choices, *lines]) + '\n')


def install(system, profiles=Path('/nix/var/nix/profiles'), loader=Path('/boot/loader'),
            pool=Path('/var/lib/airlock/closure-store')):
    core.store_entry(system)
    with tempfile.TemporaryDirectory(prefix='airlock-boot-profiles-') as directory:
        copied = Path(directory) / 'profiles'
        # Authenticated retained entries already boot independently from the ESP.
        # Do not regenerate their upstream payload copies on a constrained ESP.
        # The real profiles and retained entries remain unchanged.
        sealed = verified_sealed_systems(pool, loader.parent)
        copy_profiles(profiles, copied, sealed=sealed, selected=(profiles / 'system').resolve())
        if not (copied / 'system').exists():
            core.fail('selected host profile is unavailable for boot installation')
        # Upstream rewrites the default as part of installation. Keep its
        # loader edits private until installation succeeds, then publish entries
        # without selecting an unfinished deployment. EFI payloads remain on
        # the real ESP; retained payloads have their own independently sealed copies.
        staged_loader = Path(directory) / 'loader'
        shutil.copytree(loader, staged_loader)
        host.command('mount', '--bind', str(copied), str(profiles))
        try:
            host.command('mount', '--bind', str(staged_loader), str(loader))
            try:
                subprocess.run([system + '/bin/switch-to-configuration', 'boot'], check=True)
            finally:
                host.command('umount', str(loader))
        finally:
            host.command('umount', str(profiles))
        publish_loader(staged_loader, loader)


if __name__ == '__main__':
    install(sys.argv[1])
