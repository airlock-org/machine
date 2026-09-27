"""Host publication invariants; these tests do not require mounts or Nix."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"))
from airlock_qubes_storage._engine import builder, core, host, pool  # noqa: E402
from airlock_qubes_storage.host_boot import boot_system  # noqa: E402

A = "/nix/store/" + "0" * 32 + "-host"
B = "/nix/store/" + "1" * 32 + "-package"


def registration(path, references=(), digest="0" * 64):
    return "\n".join([path, digest, "42", "", str(len(references)), *references]) + "\n"


def test_registration_union_reuses_identity_and_rejects_conflicts():
    original = registration(B)
    successor = registration(A, [B]) + original
    merged = builder.merge_registration(original, successor)
    assert builder.registration_paths(merged) == {A, B}
    assert len(builder.registration_records(merged)) == 2
    with pytest.raises(core.EngineError, match="conflicting"):
        builder.merge_registration(original, registration(B, digest="1" * 64))
    with pytest.raises(core.EngineError, match="reference-complete"):
        builder.merge_registration(registration(A, [B]))


@pytest.mark.parametrize("cmdline", ["", "init=/bin/sh", f"init={A}/init init={B}/init",
                                     f"init={A}/specialisation/../init"])
def test_boot_rejects_ambiguous_or_unbound_init_paths(cmdline):
    with pytest.raises(core.EngineError):
        boot_system(cmdline)


def test_boot_extracts_exact_host_and_specialisation_identity():
    assert boot_system(f"quiet init={A}/init") == A
    assert boot_system(f"init={A}/specialisation/usb-lab/init") == A + "/specialisation/usb-lab"


def load_builder():
    spec = importlib.util.spec_from_file_location("builder_mount", ROOT / "lifecycle/runtime/tools/scripts/sys-dev-build-store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_additive_transition_preserves_upper_and_refuses_unknown_base(tmp_path):
    mount = load_builder()
    previous = {"generation": 1, "closure_hash": "first", "registration_sha256": "one"}
    mount.record_base(tmp_path, previous)
    upper = tmp_path / "upper"
    upper.mkdir()
    (upper / "saved-output").write_text("must survive")
    successor = {"generation": 2, "closure_hash": "second", "registration_sha256": "two",
                 "predecessors": [previous]}
    mount.record_base(tmp_path, successor)
    assert (upper / "saved-output").read_text() == "must survive"
    assert json.loads((tmp_path / "base.json").read_text()) == successor
    with pytest.raises(RuntimeError, match="base changed"):
        mount.record_base(tmp_path, previous)
    assert json.loads((tmp_path / "base.json").read_text()) == successor


def publication(tmp_path, monkeypatch):
    root = pool.ensure_pool(tmp_path / "pool")
    directory = root / "domains/dom0/generations/1"
    directory.mkdir(parents=True)
    (directory / "registration").write_text(registration(A))
    (directory / "store.erofs").write_bytes(b"metadata")
    core.atomic_json(directory / "blob-map.json", {"segments": [], "extents": [], "logical_size": 0})
    manifest = {"schema": core.SCHEMA_GENERATION, "status": "committed", "domain": "dom0",
                "generation": 1, "system": A, "store": {
                    "paths": [A], "closure_hash": "closure",
                    "canonical_metadata_image": str(directory / "store.erofs"),
                    "canonical_blob_map": str(directory / "blob-map.json")}}
    core.atomic_json(directory / "manifest.json", manifest)
    monkeypatch.setattr(host, "admit", lambda *a, **kw: {"manifest": str(directory / "manifest.json")})
    return root, directory


def test_publication_failure_keeps_pin_and_never_exposes_boot_record(tmp_path, monkeypatch):
    root, directory = publication(tmp_path, monkeypatch)
    def interrupt(name):
        if name == "host-before-boot-record":
            raise RuntimeError("power loss")
    monkeypatch.setattr(core, "maybe_failpoint", interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        host.publish(root, A, registration(A))
    assert (root / "running/dom0/1/host-retained.json").exists()
    assert not list((root / "host/boots").glob("*.json"))


def test_published_host_rejects_corrupt_metadata_and_missing_pin(tmp_path, monkeypatch):
    root, directory = publication(tmp_path, monkeypatch)
    record = host.publish(root, A, registration(A))
    assert host.validate_boot(root, A) == record
    (directory / "store.erofs").write_bytes(b"damaged")
    with pytest.raises(core.EngineError, match="checksum"):
        host.validate_boot(root, A)
    (directory / "store.erofs").write_bytes(b"metadata")
    (root / "running/dom0/1/host-retained.json").unlink()
    with pytest.raises(core.EngineError, match="pin missing"):
        host.validate_boot(root, A)


def test_upper_reference_closure_keeps_dependencies_without_cached_lower_entries():
    from airlock_qubes_storage.host_state import reference_closure
    records = builder.registration_records(registration(A, [B]) + registration(B))
    assert reference_closure(records, [A]) == {A, B}
    assert reference_closure(records, []) == set()
    with pytest.raises(core.EngineError, match="unregistered"):
        reference_closure({A: records[A]}, [A])


def test_interrupted_database_rename_restores_previous_database(tmp_path):
    from airlock_qubes_storage.host_state import recover_database
    state = tmp_path / "nix/var/nix"
    previous = state / "airlock-db-previous"
    previous.mkdir(parents=True)
    (previous / "db.sqlite").write_bytes(b"original complete database")
    pending = state / "airlock-db-pending"
    pending.mkdir()
    (pending / "partial").write_text("incomplete")
    recover_database(tmp_path)
    assert (state / "db/db.sqlite").read_bytes() == b"original complete database"
    assert not pending.exists()
    assert not previous.exists()


def test_recovery_uses_xen_efi_binding_and_rejects_damaged_initramfs(tmp_path):
    from airlock_qubes_storage import host_artifacts
    root, esp = tmp_path / "pool", tmp_path / "esp"
    entry = esp / "loader/entries/xen-generation-1.conf"
    entry.parent.mkdir(parents=True)
    entry.write_text("efi /EFI/nixos/xen-generation-1.efi\n")
    directory = esp / "EFI/nixos"
    directory.mkdir(parents=True)
    (directory / "xen-generation-1.efi").write_bytes(b"xen")
    (directory / "kernel.efi").write_bytes(b"kernel")
    (directory / "initrd.efi").write_bytes(b"initrd")
    write_xen_efi(directory / "xen-generation-1.efi", f"[xen]\nkernel=kernel.efi init={A}/init\nramdisk=initrd.efi\n")
    record = {"system": A, "generation": 1}
    core.atomic_json(root / "host/boots" / (host.key(A) + ".json"), record)
    host_artifacts.seal(root, esp)
    assert host_artifacts.verify(root, esp, record) == entry.name
    (directory / "initrd.efi").write_bytes(b"broken")
    with pytest.raises(core.EngineError, match="corrupt boot artifact"):
        host_artifacts.verify(root, esp, record)


def write_xen_efi(path, config):
    import struct
    payload = config.encode() + b"\0"
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 70, 1)
    data[88:96] = b".config\0"
    struct.pack_into("<II", data, 104, len(payload), 512)
    path.write_bytes(data + payload)


def test_xen_config_rejects_truncated_embedded_section(tmp_path):
    from airlock_qubes_storage import host_artifacts
    binary = tmp_path / 'xen.efi'
    write_xen_efi(binary, f'kernel=kernel.efi init={A}/init\nramdisk=initrd.efi\n')
    binary.write_bytes(binary.read_bytes()[:-8])
    with pytest.raises(core.EngineError, match='truncated Xen EFI configuration'):
        host_artifacts.xen_config(binary)


@pytest.mark.parametrize('mount_result', [0, -1])
def test_host_overlay_mount_uses_post_switch_root_paths(monkeypatch, mount_result):
    import errno
    from unittest.mock import Mock
    from airlock_qubes_storage import host_boot
    events = []
    mount = Mock(side_effect=lambda *args: events.append(('mount', args)) or mount_result)
    libc = Mock(mount=mount)
    monkeypatch.setattr(host_boot.ctypes, 'CDLL', lambda *a, **kw: events.append(('libc',)) or libc)
    monkeypatch.setattr(host_boot.ctypes, 'get_errno', lambda: errno.EPERM)
    monkeypatch.setattr(host_boot.os, 'chroot', lambda path: events.append(('chroot', path)))
    monkeypatch.setattr(host_boot.os, 'chdir', lambda path: events.append(('chdir', path)))
    if mount_result:
        with pytest.raises(OSError) as failure:
            host_boot.mount_overlay_in_root('/sysroot')
        assert failure.value.errno == errno.EPERM
    else:
        host_boot.mount_overlay_in_root('/sysroot')
    assert events[:3] == [('libc',), ('chroot', '/sysroot'), ('chdir', '/')]
    source, target, fstype, flags, options = events[3][1]
    assert (source, target, fstype, flags) == (b'overlay', b'/nix/store', b'overlay', 0)
    assert b'/sysroot' not in options
    assert b'lowerdir=/var/lib/airlock/host-store/lower/nix/store' in options
    assert b'upperdir=/var/lib/airlock/host-store/upper' in options
