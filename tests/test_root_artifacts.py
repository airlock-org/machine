"""Sparse root integrity, retention and isolation without mounts or Nix builds."""

import io
import importlib.util
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"))

from airlock_qubes_storage import root_format as fmt  # noqa: E402
from airlock_qubes_storage._engine import core, pool, root_images  # noqa: E402


def sparse_image(path, marker=b"boot"):
    # Only the format marker matters to the codec; no filesystem is mounted.
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(1080)
        stream.write(b"\x53\xef")
        stream.seek(2 * fmt.BLOCK)
        stream.write(marker)
    return path


def package(path, target):
    with target.open("wb") as stream:
        return fmt.encode(path, stream)


def test_sparse_identity_is_independent_of_physical_allocation(tmp_path):
    source = sparse_image(tmp_path / "source")
    dense = tmp_path / "dense"
    dense.write_bytes(source.read_bytes())
    payload = tmp_path / "payload"
    descriptor = package(source, payload)
    assert descriptor == fmt.encode(dense)
    assert descriptor["payload_bytes"] < descriptor["logical_bytes"] // 10
    restored = tmp_path / "restored"
    fmt.decode(payload, restored, descriptor)
    assert restored.read_bytes() == source.read_bytes()
    assert restored.stat().st_blocks * 512 < restored.stat().st_size // 10


@pytest.mark.parametrize("damage", ["checksum", "truncated", "overlap", "format", "abi"])
def test_invalid_root_is_never_published(tmp_path, damage):
    source = sparse_image(tmp_path / "source")
    payload = tmp_path / "payload"
    descriptor = package(source, payload)
    if damage == "checksum":
        descriptor["sha256"] = "0" * 64
    elif damage == "truncated":
        payload.write_bytes(payload.read_bytes()[:-1])
    elif damage == "overlap":
        data = bytearray(payload.read_bytes())
        second = len(fmt.MAGIC) + 8 + 12 + fmt.BLOCK
        data[second:second + 8] = bytes(8)
        payload.write_bytes(data)
    else:
        descriptor["format" if damage == "format" else "boot_abi"] = "unsupported"
    root = pool.ensure_pool(tmp_path / "pool")
    with pytest.raises((ValueError, core.EngineError)):
        root_images.install_root(root, payload, descriptor)
    assert not list((root / "root-artifacts").glob("*/root.img"))
    assert not list((root / "tmp").iterdir())


def test_shared_base_keeps_separate_writes_and_generation_pins(tmp_path):
    root = pool.ensure_pool(tmp_path / "pool")
    payload = tmp_path / "payload"
    descriptor = package(sparse_image(tmp_path / "source"), payload)
    base = root_images.install_root(root, payload, descriptor)
    # The VM volume importer copies a base into distinct native volumes.
    disks = [tmp_path / "vm-a-root", tmp_path / "vm-b-root"]
    for disk in disks:
        shutil.copyfile(base, disk)
    with disks[0].open("r+b") as stream:
        stream.seek(2 * fmt.BLOCK)
        stream.write(b"private change")
    assert len({path.stat().st_ino for path in [base, *disks]}) == 3
    assert fmt.encode(disks[1]) == descriptor == fmt.encode(base)
    assert fmt.encode(disks[0]) != descriptor
    manifests = []
    for domain in ("vm-a", "vm-b"):
        manifest = root / "domains" / domain / "generations/1/manifest.json"
        manifest.parent.mkdir(parents=True)
        core.atomic_json(manifest, {"root": {"artifact": descriptor}})
        manifests.append(manifest)
    manifests[0].unlink()
    assert root_images.gc_roots(root) == []
    assert root_images.reusable_root(root, descriptor) == base
    manifests[1].unlink()
    assert root_images.gc_roots(root) == [descriptor["sha256"]]
    assert disks[1].exists()


def test_writable_or_corrupt_retained_artifact_fails_closed(tmp_path):
    root = pool.ensure_pool(tmp_path / "pool")
    payload = tmp_path / "payload"
    descriptor = package(sparse_image(tmp_path / "source"), payload)
    base = root_images.install_root(root, payload, descriptor)
    base.chmod(0o644)
    with pytest.raises(core.EngineError, match="integrity"):
        root_images.reusable_root(root, descriptor)
    with base.open("r+b") as stream:
        stream.seek(2 * fmt.BLOCK)
        stream.write(b"corrupt")
    base.chmod(0o444)
    with pytest.raises(core.EngineError, match="integrity"):
        root_images.reusable_root(root, descriptor)


def test_changed_contents_get_a_different_identity(tmp_path):
    first = sparse_image(tmp_path / "first", b"boot-v1")
    second = sparse_image(tmp_path / "second", b"boot-v2")
    assert fmt.encode(first)["sha256"] != fmt.encode(second)["sha256"]
    with pytest.raises(ValueError, match="regular file"):
        alias = tmp_path / "alias"
        alias.symlink_to(first)
        fmt.encode(alias, io.BytesIO())


@pytest.mark.parametrize("alias", ["none", "same-volume", "snapshot-source", "root-private"])
def test_native_volume_ownership_rejects_cross_vm_aliases(alias):
    path = Path(__file__).resolve().parents[1] / "lifecycle/runtime/tools/scripts/nix-domain-storage.py"
    spec = importlib.util.spec_from_file_location("root_storage_helper", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def volume(vid):
        return SimpleNamespace(pool="native", vid=vid, rw=True, save_on_stop=True,
                               snap_on_start=False, source=None)

    first = SimpleNamespace(name="vm-a", volumes={
        "root": volume("a/root"), "private": volume("a/private")})
    second = SimpleNamespace(name="vm-b", volumes={
        "root": volume("b/root"), "private": volume("b/private")})
    if alias == "same-volume":
        second.volumes["root"].vid = "a/root"
    elif alias == "snapshot-source":
        second.volumes["root"].source = first.volumes["root"]
    elif alias == "root-private":
        first.volumes["private"].vid = "a/root"
    app = SimpleNamespace(domains=[first, second])
    if alias == "none":
        helper.validate_native_volume_isolation(app, first)
        helper.validate_native_volume_isolation(app, second)
    else:
        with pytest.raises(SystemExit):
            helper.validate_native_volume_isolation(app, first)
