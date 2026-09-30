"""Current pool formats reject obsolete state without converting it."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src'))
from airlock_qubes_storage._engine import core, pool, views  # noqa: E402


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def test_current_pool_can_be_reopened_without_rewriting_metadata(tmp_path):
    root = pool.ensure_pool(tmp_path / 'pool')
    before = snapshot(root)
    assert pool.ensure_pool(root) == root
    assert snapshot(root) == before


@pytest.mark.parametrize('layout', ['single-blob', 'unsegmented-manifest', 'wrong-schema'])
def test_unsupported_pool_fails_before_creating_or_rewriting_files(tmp_path, layout):
    root = tmp_path / 'pool'
    root.mkdir()
    if layout == 'single-blob':
        (root / 'objects').mkdir()
        (root / 'objects/data.blob').write_bytes(b'retained data')
    elif layout == 'unsegmented-manifest':
        core.atomic_json(root / 'MANIFEST.json', {
            'schema': core.SCHEMA_POOL, 'object_key': 'nix-store-basename',
            'object_store': {'data': 'objects/data.blob'},
        })
    else:
        core.atomic_json(root / 'MANIFEST.json', {'schema': 'unknown'})
    before = snapshot(root)
    directories = sorted(path.relative_to(root) for path in root.rglob('*') if path.is_dir())
    with pytest.raises(core.EngineError, match='unsupported .*pool'):
        pool.ensure_pool(root)
    assert snapshot(root) == before
    assert sorted(path.relative_to(root) for path in root.rglob('*') if path.is_dir()) == directories


@pytest.mark.parametrize('blob_map', [
    {'central_blob': 'objects/data.blob', 'extents': []},
    {'extents': [{'central_offset': 0, 'length': 512}]},
    {'extents': [{'segment_id': '000001', 'central_offset': 0, 'length': 512}]},
])
def test_single_blob_maps_are_rejected(blob_map):
    with pytest.raises(core.EngineError, match='obsolete single-blob'):
        views.segment_path_map_from_blob_map(blob_map)
