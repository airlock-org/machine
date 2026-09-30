"""Real EROFS reads across additive indexes, fallback and interrupted publication."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src'))
from airlock_qubes_storage._engine import builder, core, generations, host, maintenance, objects, pool, segments, views  # noqa: E402


@pytest.fixture
def index_store(tmp_path, monkeypatch):
    if not shutil.which('mkfs.erofs') or not shutil.which('fsck.erofs'):
        pytest.skip('requires patched airlock-erofs-utils (run pytest through nix shell)')
    probe = subprocess.run(['mkfs.erofs', '--tar-offset=513', 'unused', 'unused'],
                           capture_output=True, text=True)
    if 'invalid tar index offset' not in probe.stderr:
        pytest.skip('requires Airlock tar-offset support')
    monkeypatch.setenv('AIRLOCK_CLOSURE_STORE_INDEX_SOURCE', 'file')
    root = pool.ensure_pool(tmp_path / 'pool')
    physical = tmp_path / 'physical'
    physical.mkdir()

    def add(letter, payload):
        path = '/nix/store/' + letter * 32 + '-object'
        source = physical / Path(path).name
        source.mkdir()
        (source / 'payload').write_bytes(payload)
        (source / ('long-' + 'x' * 120)).write_text('long pathname')
        (source / 'link').symlink_to('payload')
        objects.admit_object(root, path, physical, 'fixture', 16384)
        return path

    def build(paths, number, previous=None):
        directory = root / 'domains/dom0/generations' / str(number)
        directory.mkdir(parents=True, exist_ok=True)
        result = views.materialize_view(root, 'dom0', number, paths,
            hashlib.sha256(str(number).encode()).hexdigest(), core.VIEW_BACKEND_EROFS_OBJECTMAP,
            'test' + str(number), 16384, trusted_paths=set(paths), previous_index=previous)
        store = dict(result, paths=paths, integrity=views.view_integrity(result))
        return store

    def extract(store, name):
        tar = tmp_path / (name + '.tar')
        views.reconstruct_blob_view(core.read_json(store['canonical_blob_map']), tar)
        target = tmp_path / name
        subprocess.run(['fsck.erofs', '--device=' + str(tar), '--extract=' + str(target),
                        store['canonical_metadata_image']], check=True, capture_output=True)
        return target

    return root, add, build, extract


def test_additive_index_reads_old_and_new_bytes_without_mutating_base(index_store):
    _, add, build, extract = index_store
    old = add('b', b'original' * 99)
    first = build([old], 1)
    before = Path(first['canonical_metadata_image']).read_bytes()
    new = add('0', b'new' * 171)  # Sorts before the inherited object.
    second = build([new, old], 2, first)
    assert second['preparation']['index_reused_objects'] == 1
    assert second['preparation']['index_new_objects'] == 1
    assert second['index_depth'] == 1
    assert Path(first['canonical_metadata_image']).read_bytes() == before
    files = extract(second, 'next')
    assert (files / Path(old).name / 'payload').read_bytes() == b'original' * 99
    assert (files / Path(new).name / 'payload').read_bytes() == b'new' * 171
    assert (files / Path(old).name / 'link').readlink() == Path('payload')
    assert (files / Path(new).name / ('long-' + 'x' * 120)).read_text() == 'long pathname'
    assert (extract(first, 'previous') / Path(old).name / 'payload').read_bytes() == b'original' * 99


@pytest.mark.parametrize('reason', ['removed-object', 'legacy', 'depth'])
def test_incompatible_predecessor_uses_full_index(index_store, reason):
    _, add, build, extract = index_store
    a = add('a', b'keep')
    b = add('b', b'remove')
    first = build([a, b], 1)
    paths = [a]
    if reason == 'legacy':
        first.pop('integrity')
        paths = [a, b]
    if reason == 'depth':
        first['index_depth'] = 16
        paths = [a, b]
    second = build(paths, 2, first)
    assert second['index_depth'] == 0
    assert second['preparation']['index_reused_objects'] == 0
    files = extract(second, 'full')
    assert (files / Path(a).name / 'payload').read_bytes() == b'keep'
    assert (files / Path(b).name).exists() == (b in paths)


def test_corrupt_predecessor_fails_before_publishing_new_index(index_store):
    root, add, build, _ = index_store
    a = add('a', b'original')
    first = build([a], 1)
    Path(first['canonical_metadata_image']).chmod(0o644)
    Path(first['canonical_metadata_image']).write_bytes(b'damaged')
    b = add('b', b'new')
    with pytest.raises(core.EngineError, match='checksum'):
        build([a, b], 2, first)
    target = root / 'views/by-closure' / hashlib.sha256(b'2').hexdigest()
    assert not (target / 'store.erofs').exists()


def test_interrupted_incremental_index_preserves_base_and_retries(index_store, monkeypatch):
    _, add, build, extract = index_store
    a = add('a', b'original')
    first = build([a], 1)
    before = Path(first['canonical_metadata_image']).read_bytes()
    b = add('b', b'new')

    def interrupt(name):
        if name == 'view-after-metadata-build':
            raise core.EngineError('power loss')

    with monkeypatch.context() as m:
        m.setattr(views, 'maybe_failpoint', interrupt)
        with pytest.raises(core.EngineError, match='power loss'):
            build([a, b], 2, first)
    assert Path(first['canonical_metadata_image']).read_bytes() == before
    second = build([a, b], 2, first)
    assert (extract(second, 'retry') / Path(b).name / 'payload').read_bytes() == b'new'


def test_host_and_controller_publication_reuse_verified_indexes(index_store, monkeypatch):
    root, add, _, extract = index_store
    physical = root.parent / 'physical'
    admit_generation = generations.admit_generation

    def admit(args):
        args.physical_store_root = str(physical)
        return admit_generation(args)

    monkeypatch.setattr(generations, 'admit_generation', admit)
    monkeypatch.setattr(generations, 'run_verify', lambda *a, **kw: {'status': 'passed'})

    def registration(paths):
        return ''.join('\n'.join([path, '0' * 64, '1', '', '0']) + '\n' for path in paths)

    a = add('a', b'previous')
    first = host.publish(root, a, registration([a]))
    core.atomic_json(root / 'host/running.json', first)
    initial = host.admit(root, 'sys-ctrl-builder', a, registration([a]))
    builder.prepare_builder(root, initial, Path(initial['manifest']).parent / 'registration')
    previous_builder = core.read_json(root / 'builders/sys-ctrl.json')
    b = add('b', b'next')
    statistics = {}
    second = host.publish(root, b, registration([a, b]), statistics=statistics)
    assert host.validate_boot(root, a) == first
    assert host.validate_boot(root, b) == second
    assert statistics['host_index_reused_objects'] == 1
    host.refresh_builder(root, 'sys-ctrl', registration([a, b]), statistics=statistics)
    assert statistics['controller_index_reused_objects'] == 1
    current = core.read_json(root / 'builders/sys-ctrl.json')
    assert current['predecessors'][-1]['generation'] == previous_builder['generation']
    manifest = core.read_json(second['manifest'])
    assert (extract(manifest['store'], 'published') / Path(b).name / 'payload').read_bytes() == b'next'
    directory = root / 'domains/sys-ctrl-builder/generations' / str(current['generation'])
    manifest = core.read_json(directory / 'manifest.json')
    image = dict(manifest['store'], canonical_metadata_image=current['metadata_image'])
    files = extract(image, 'controller')
    assert (files / Path(a).name / 'payload').read_bytes() == b'previous'
    assert (files / Path(b).name / 'payload').read_bytes() == b'next'
    assert builder.registration_paths((files / '.airlock-builder/registration').read_text()) == {a, b}


def test_incremental_index_survives_physical_extent_relocation(index_store):
    root, add, build, extract = index_store
    a = add('a', b'original')
    first = build([a], 1)
    entry = Path(a).name
    before = core.read_json(segments.object_blob_meta_path(root, entry))
    source = root / 'tmp/relocate.tar'
    with Path(before['segment_blob']).open('rb') as stream:
        stream.seek(before['segment_offset'])
        source.write_bytes(stream.read(before['size']))
    moved = objects.append_file_to_segment(root, source, entry, 'move', 16384)
    assert maintenance.publish_blob_location(root, entry, segments.location_from_extent(moved), 'move', 'test')
    assert maintenance.refresh_blob_map_locations(root, first['canonical_blob_map'], {entry})
    assert views.view_integrity(first) == first['integrity']
    b = add('b', b'new')
    second = build([a, b], 2, first)
    assert second['preparation']['index_reused_objects'] == 1
    files = extract(second, 'relocated')
    assert (files / Path(a).name / 'payload').read_bytes() == b'original'
    assert (files / Path(b).name / 'payload').read_bytes() == b'new'
