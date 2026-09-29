"""Boot selection isolation with real metadata validation and emulated mounts."""
import ctypes
import errno
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src'))
from airlock_qubes_storage import host_boot, host_state  # noqa: E402
from airlock_qubes_storage._engine import builder, core, host, pool, prepared, state  # noqa: E402

A = '/nix/store/' + '0' * 32 + '-host'
B = '/nix/store/' + '1' * 32 + '-new-host'
C = '/nix/store/' + '2' * 32 + '-saved-output'
D = '/nix/store/' + '3' * 32 + '-dependency'


def registration(path, references=()):
    return '\n'.join([path, '0' * 64, '42', '', str(len(references)), *references]) + '\n'


@pytest.fixture
def boot_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'POOL', '/pool')
    monkeypatch.setattr(host, 'HOST', '/host')
    root = pool.ensure_pool(tmp_path / 'pool')
    for generation, system in ((1, A), (2, B)):
        directory = state.generation_manifest_path(root, 'dom0', generation).parent
        directory.mkdir(parents=True)
        metadata = directory / 'store.erofs'
        metadata.write_bytes(f'view {generation}'.encode())
        blob = directory / 'blob-map.json'
        core.atomic_json(blob, {'segments': [], 'extents': [], 'logical_size': 0})
        manifest = {'schema': core.SCHEMA_GENERATION, 'status': 'committed', 'domain': 'dom0',
                    'generation': generation, 'system': system, 'store': {
                        'paths': [system], 'closure_hash': f'closure-{generation}',
                        'canonical_metadata_image': str(metadata), 'canonical_blob_map': str(blob)}}
        core.atomic_json(directory / 'manifest.json', manifest)
        # The mocked admission must persist the registration, as host.admit
        # does before publication checks and flushes the generation files.
        core.atomic_write(directory / 'registration', registration(system), mode=0o444)
        monkeypatch.setattr(host, 'admit', lambda *a, directory=directory, **kw:
                            {'manifest': str(directory / 'manifest.json')})
        host.publish(root, system, registration(system))
        target = tmp_path / system.lstrip('/')
        target.mkdir(parents=True)
        (target / 'init').write_text('init')
    for path in root.rglob('*.json'):
        core.atomic_json(path, prepared.relocate(core.read_json(path), str(root), '/pool'))
    # Empty blob maps contain no relocated paths, so their checksums are stable.
    commands, databases, lower_views = [], [], []
    monkeypatch.setattr(host, 'command', lambda *args, **kwargs: commands.append((args, kwargs)) or '')
    monkeypatch.setattr(host_boot.views, 'create_dm_view_for_blob_map',
                        lambda *a: {'device': '/dev/mapper/test'})
    monkeypatch.setattr(host_boot, 'mount_lower_view', lambda *args: lower_views.append(args))
    monkeypatch.setattr(host_state, 'replace_database', lambda root, text, **kwargs:
                        databases.append((kwargs['state_root'], builder.registration_paths(text))))
    return tmp_path, root, commands, databases, lower_views


@pytest.mark.parametrize('fault', ['record', 'metadata', 'missing-view', 'pin', 'database'])
def test_older_boot_does_not_open_newer_generation(boot_tree, fault):
    sysroot, root, commands, databases, lower_views = boot_tree
    newer = root / 'domains/dom0/generations/2'
    if fault == 'record':
        (root / 'host/boots' / (host.key(B) + '.json')).write_text('invalid JSON')
    elif fault == 'metadata':
        (newer / 'store.erofs').write_bytes(b'corrupt')
    elif fault == 'missing-view':
        (newer / 'store.erofs').unlink()
    elif fault == 'pin':
        state.running_ref_path(root, 'dom0', 2, 'host-retained').unlink()
    else:
        database = sysroot / 'host/generations/2/nix/var/nix/db/db.sqlite'
        database.parent.mkdir(parents=True)
        database.write_bytes(b'corrupt database')
    host_boot.assemble(sysroot, f'init={A}/init')
    assert [view[0] for view in lower_views] == [sysroot / 'host/bases/1']
    assert databases == [(sysroot / 'host/generations/1', {A})]
    erofs = [args for args, _ in commands if args[:3] == ('mount', '-t', 'erofs')]
    assert len(erofs) == 1 and erofs[0][-1].endswith('/bases/1')
    assert json.loads((root / 'host/running.json').read_text())['system'] == A


def test_broken_selected_view_still_fails_closed(boot_tree):
    sysroot, root, commands, databases, _ = boot_tree
    (root / 'domains/dom0/generations/1/store.erofs').write_bytes(b'corrupt')
    with pytest.raises(core.EngineError, match='checksum'):
        host_boot.assemble(sysroot, f'init={A}/init')
    assert not commands and not databases


def test_returning_generation_uses_its_own_writable_state(boot_tree, monkeypatch):
    sysroot, _, commands, databases, _ = boot_tree
    own = sysroot / 'host/generations/1'
    upper = own / 'upper'
    upper.mkdir(parents=True)
    (upper / Path(C).name).write_text('saved output')
    db = own / 'nix/var/nix/db/db.sqlite'
    db.parent.mkdir(parents=True)
    db.write_bytes(b'own database')
    other = sysroot / 'host/generations/2/upper'
    other.mkdir(parents=True)
    (other / Path(D).name).write_text('newer output')
    def dump(root):
        assert root == own
        return registration(A) + registration(C, [A])
    monkeypatch.setattr(host_state, 'dump_registration', dump)
    host_boot.assemble(sysroot, f'init={A}/init')
    assert databases == [(own, {A, C})]
    assert (upper / Path(C).name).read_text() == 'saved output'
    assert (other / Path(D).name).read_text() == 'newer output'
    assert (('mount', '--bind', str(own), str(sysroot / 'host/active')), {}) in commands
    assert (('mount', '--bind', str(own / 'nix/var/nix/db'), str(sysroot / 'nix/var/nix/db')), {}) in commands


def test_publication_freezes_additions_and_their_lower_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'HOST', str(tmp_path))
    upper = tmp_path / 'active/upper'
    upper.mkdir(parents=True)
    (upper / Path(C).name).write_text('saved output')
    (upper / 'unregistered-import').write_text('partial import')
    monkeypatch.setattr(host, 'command', lambda *a, **kw:
                        registration(A) + registration(B) + registration(C, [D]) + registration(D))
    frozen = host.publication_registration(B)
    assert builder.registration_paths(frozen) == {B, C, D}
    assert A not in frozen


def test_lower_mount_failure_reports_errno(monkeypatch):
    mount = Mock(return_value=-1)
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **kw: Mock(mount=mount))
    monkeypatch.setattr(ctypes, 'get_errno', lambda: errno.EINVAL)
    with pytest.raises(OSError) as failure:
        host_boot.mount_lower_view('/base', '/scaffold', '/target')
    assert failure.value.errno == errno.EINVAL
    assert 'lowerdir=/base:/scaffold' in str(failure.value)
    assert mount.call_args.args[3] == 1  # MS_RDONLY


def test_mount_failure_report_precedes_cleanup_and_preserves_original(boot_tree, monkeypatch):
    sysroot, _, commands, _, _ = boot_tree
    events = []
    original = OSError(errno.EINVAL, 'original mount error')
    def mount(*args):
        raise original
    def cleanup(*args, **kwargs):
        if args[0] == 'umount':
            events.append('cleanup')
            raise RuntimeError('cleanup also failed')
        return ''
    monkeypatch.setattr(host_boot, 'mount_lower_view', mount)
    monkeypatch.setattr(host_boot, 'record_mount_failure', lambda *a: events.append('report'))
    monkeypatch.setattr(host, 'command', cleanup)
    with pytest.raises(OSError) as failure:
        host_boot.assemble(sysroot, f'init={A}/init')
    assert failure.value is original
    assert events == ['report', 'cleanup']


def machine_binding(tmp_path, monkeypatch, *, selection=True, phase='complete'):
    from airlock_qubes_storage import host_machine
    monkeypatch.setattr(host, 'HOST', '/host')
    monkeypatch.setattr(host_machine, 'MACHINE', '/machine')
    record = {'system': A, 'generation': 1}
    pointer = {'release': '/machine/candidates/old', 'transaction': 'old', 'profile': 'usb-lab'}
    core.atomic_json(tmp_path / 'machine/candidates/old/release.json', {'host': A})
    core.atomic_json(tmp_path / 'machine/transactions/old/status.json', {'phase': phase, 'revision': 'old', 'release': pointer['release']})
    core.atomic_json(host_machine.binding_path(tmp_path / 'host', record), {
        'schema': 'airlock.host-machine.v1', 'system': A,
        'selection': pointer if selection else None, 'staged': {'usr-home': 1},
    })
    return host_machine, record, pointer


def test_old_boot_restores_own_machine_selection_without_reading_newer(tmp_path, monkeypatch):
    module, record, pointer = machine_binding(tmp_path, monkeypatch)
    (tmp_path / 'machine/selection.json').write_text('corrupt newer pointer')
    (tmp_path / 'machine/latest.json').write_text('corrupt newer status')
    module.restore(tmp_path, record)
    assert core.read_json(tmp_path / 'machine/selection.json') == pointer
    assert core.read_json(tmp_path / 'machine/latest.json')['revision'] == 'old'
    assert (tmp_path / 'machine/controller').readlink() == Path(A + '/sw/bin/airlock-machine-deploy')


def test_image_rollback_restores_seed_guest_generations(tmp_path, monkeypatch):
    module, record, _ = machine_binding(tmp_path, monkeypatch, selection=False)
    (tmp_path / 'machine/selection.json').write_text('unreadable newer pointer')
    module.restore(tmp_path, record)
    assert not (tmp_path / 'machine/selection.json').exists()
    assert core.read_json(tmp_path / 'host/boot-staged.json') == {'usr-home': 1}


def test_incomplete_chosen_deployment_fails_without_replacing_selection(tmp_path, monkeypatch):
    module, record, _ = machine_binding(tmp_path, monkeypatch, phase='configuring')
    pointer = tmp_path / 'machine/selection.json'
    pointer.write_text('original pointer')
    with pytest.raises(core.EngineError, match='incomplete'):
        module.restore(tmp_path, record)
    assert pointer.read_text() == 'original pointer'


def test_published_generation_without_machine_binding_is_rejected(tmp_path, monkeypatch):
    from airlock_qubes_storage import host_machine
    monkeypatch.setattr(host, 'HOST', '/host')
    monkeypatch.setattr(host_machine, 'MACHINE', '/machine')
    with pytest.raises(core.EngineError, match='no machine boot binding'):
        host_machine.restore(tmp_path, {'system': B, 'generation': 2})


def test_checkpoint_captures_initial_guest_selection(tmp_path):
    from airlock_qubes_storage import host_machine
    root = tmp_path / 'pool'
    guest = root / 'domains/usr-home'
    guest.mkdir(parents=True)
    (guest / 'current').symlink_to('generations/1')
    record = {'generation': 1, 'system': A}
    host_machine.save(root, tmp_path / 'host', tmp_path / 'machine', record)
    binding = core.read_json(host_machine.binding_path(tmp_path / 'host', record))
    assert binding['selection'] is None
    assert binding['staged'] == {'usr-home': 1}


def test_boot_install_profile_filter_does_not_modify_real_history(tmp_path):
    from airlock_qubes_storage.host_install import copy_profiles
    real, filtered = tmp_path / 'profiles', tmp_path / 'filtered'
    real.mkdir()
    available = tmp_path / 'present-system'
    available.mkdir()
    (real / 'system-1-link').symlink_to(tmp_path / 'absent-old-store-view')
    (real / 'system-2-link').symlink_to(available)
    (real / 'system').symlink_to('system-2-link')
    copy_profiles(real, filtered)
    assert (real / 'system-1-link').is_symlink()
    assert not (filtered / 'system-1-link').is_symlink()
    assert (filtered / 'system').resolve() == available
    assert (filtered / 'system').readlink() == Path('system-2-link')


def test_live_metadata_seed_includes_committed_wal_and_is_not_overwritten(tmp_path):
    import sqlite3
    source, target = tmp_path / 'source.sqlite', tmp_path / 'generation/seed.sqlite'
    connection = sqlite3.connect(source)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE evidence (value TEXT)')
        connection.execute("INSERT INTO evidence VALUES ('original')")
        connection.commit()
        host_state.seed_metadata(source, target)
        connection.execute("UPDATE evidence SET value='newer generation'")
        connection.commit()
        host_state.seed_metadata(source, target)
        with sqlite3.connect(target) as snapshot:
            assert snapshot.execute('SELECT value FROM evidence').fetchall() == [('original',)]
    finally:
        connection.close()
