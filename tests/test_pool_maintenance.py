"""Maintenance uses real segment files and mappings, without VM or mount fixtures."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src'))
from airlock_qubes_storage._engine import cli, core, maintenance, objects, pool, segments, state


@pytest.fixture
def storage(tmp_path):
    root = pool.ensure_pool(tmp_path / 'pool')
    (root / 'admission.lock').touch()

    def add(letter, data):
        entry = letter * 32 + '-object'
        source = tmp_path / entry
        source.write_bytes(data)
        extent = objects.append_file_to_segment(root, source, entry, 'fixture', 16384)
        core.atomic_json(segments.object_blob_meta_path(root, entry), dict(extent, entry=entry))
        return entry, extent

    def view(name, entry, extent):
        path = root / 'views/by-closure' / name / 'store.blob-map.json'
        core.atomic_json(path, {
            'view_backend': core.VIEW_BACKEND_EROFS_OBJECTMAP,
            'extents': [dict(object=entry, segment_id=extent['segment_id'],
                             segment_blob=extent['segment_blob'],
                             segment_offset=extent['segment_offset'], logical_offset=0,
                             length=extent['size'], object_sha256=extent['sha256'])],
            'segments': [], 'segment_count': 0,
        })
        core.atomic_json(root / 'domains' / name / 'generations/1/manifest.json', {
            'schema': core.SCHEMA_GENERATION, 'status': 'committed',
            'domain': name, 'generation': 1, 'retention': {'retain': True},
            'store': {'paths': ['/nix/store/' + entry], 'canonical_blob_map': str(path)},
        })
        maintenance.refresh_blob_map_locations(root, path)
        return path

    return root, add, view


def compact(root):
    return cli.run(['compact', '--pool-root', str(root), '--segment-target-bytes', '16384'])


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_no_work_preserves_open_segment_and_maps_and_verifies_once(storage):
    root, add, view = storage
    entry, extent = add('a', b'a' * 4096)
    for name in ('host', 'builder', 'guest'):
        view(name, entry, extent)
    before = snapshot(root)
    first = compact(root)
    assert first['noop'] and first['transaction'] is None
    assert first['verification']['payload_bytes_hashed'] == extent['size']
    assert first['verification']['cache_hits'] >= 1
    assert snapshot(root) == before
    # Verification must not be cached across invocations.
    second = compact(root)
    assert second['verification']['payload_bytes_hashed'] == extent['size']
    assert snapshot(root) == before


@pytest.mark.parametrize('mutation', ['overwrite', 'replace', 'truncate', 'digest'])
def test_verification_cache_rejects_changed_file_or_expected_digest(storage, mutation):
    root, add, _ = storage
    _, extent = add('a', b'a' * 4096)
    blob = Path(extent['segment_blob'])

    @segments.with_payload_verification
    def verify_twice():
        args = (root, extent['segment_id'], extent['segment_offset'], extent['size'])
        segments.verify_segment_region(*args, extent['sha256'], 'before')
        old = blob.stat()
        digest = extent['sha256']
        if mutation == 'overwrite':
            blob.write_bytes(b'b' * 4096)
            os.utime(blob, ns=(old.st_atime_ns, old.st_mtime_ns))
        elif mutation == 'replace':
            replacement = blob.with_suffix('.replacement')
            replacement.write_bytes(b'b' * 4096)
            os.replace(replacement, blob)
        elif mutation == 'truncate':
            blob.write_bytes(b'a')
        else:
            digest = '0' * 64
        with pytest.raises(core.EngineError, match='hash mismatch|truncated'):
            segments.verify_segment_region(*args, digest, 'after')
        return {}

    verify_twice()


def relocatable(storage):
    root, add, view = storage
    entry, extent = add('a', b'a' * 4096)
    add('b', b'b' * 8192)  # Dead space in the same segment.
    segments.seal_segment_locked(root, extent['segment_id'], 'fixture', 'test')
    first = view('host', entry, extent)
    second = view('builder', entry, extent)
    other, other_extent = add('c', b'c' * 4096)
    untouched = view('unrelated', other, other_extent)
    pin = root / 'running/host/1/root.json'
    state.mark_running_ref(root, 'host', 1, 'root')
    return root, entry, extent, first, second, untouched, pin


def test_relocation_refreshes_only_affected_maps_and_preserves_running_extent(storage):
    root, entry, old, first, second, untouched, pin = relocatable(storage)
    saved = (untouched.read_bytes(), untouched.stat().st_mtime_ns)
    result = compact(root)
    assert len(result['objects_moved']) == 1
    assert result['objects_moved'][0]['entry'] == entry
    assert set(result['views_refreshed']) == {str(first), str(second)}
    assert (untouched.read_bytes(), untouched.stat().st_mtime_ns) == saved
    new = core.read_json(first)['extents'][0]
    assert new['segment_id'] != old['segment_id']
    segments.verify_segment_region(root, new['segment_id'], new['segment_offset'],
                                   new['length'], new['object_sha256'], 'new')
    assert Path(old['segment_blob']).exists()  # Running mapping still pins old bytes.
    pin.unlink()
    collected = cli.run(['gc', '--pool-root', str(root)])
    assert old['segment_id'] in collected['removed_segments']
    assert segments.segment_blob_path(root, new['segment_id']).exists()
    assert (untouched.read_bytes(), untouched.stat().st_mtime_ns) == saved


@pytest.mark.parametrize('failpoint', ['compact-after-object-copy', 'compact-after-location-publish'])
def test_interrupted_relocation_recovers_maps_and_is_retryable(storage, monkeypatch, failpoint):
    root, entry, old, first, second, _, pin = relocatable(storage)
    monkeypatch.setenv('AIRLOCK_CLOSURE_STORE_FAILPOINT', failpoint)
    with pytest.raises(core.EngineError, match='simulated failure'):
        compact(root)
    monkeypatch.delenv('AIRLOCK_CLOSURE_STORE_FAILPOINT')
    recovered = cli.run(['recover', '--pool-root', str(root)])
    assert recovered['status'] == 'passed'
    assert not list((root / 'tmp').iterdir())
    result = compact(root)
    assert result['status'] == 'passed'
    current = segments.object_extent_from_meta(root, core.read_json(
        segments.object_blob_meta_path(root, entry)), 'recovered')
    for path in (first, second):
        assert core.read_json(path)['extents'][0]['segment_id'] == current['segment_id']
    assert Path(old['segment_blob']).exists() and pin.exists()


@pytest.mark.parametrize('pin_type', ['host-retained', 'builder-persistent'])
def test_persistent_pin_keeps_exact_map_and_old_payload_through_compaction(storage, pin_type):
    root, _, old, pinned_map, other_map, _, _ = relocatable(storage)
    state.mark_running_ref(root, 'host', 1, pin_type)
    saved = (pinned_map.read_bytes(), pinned_map.stat().st_mtime_ns)
    result = compact(root)
    assert result['objects_moved']
    assert str(other_map) in result['views_refreshed']
    assert str(pinned_map) not in result['views_refreshed']
    cli.run(['recover', '--pool-root', str(root)])
    cli.run(['gc', '--pool-root', str(root)])
    assert (pinned_map.read_bytes(), pinned_map.stat().st_mtime_ns) == saved
    assert Path(old['segment_blob']).exists()
    segments.verify_segment_region(root, old['segment_id'], old['segment_offset'],
                                   old['size'], old['sha256'], 'retained')
