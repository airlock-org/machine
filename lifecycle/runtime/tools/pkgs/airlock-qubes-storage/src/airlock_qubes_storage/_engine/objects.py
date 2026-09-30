import fcntl
import os
from pathlib import Path


from .core import (
    OBJECT_BLOB_ALIGNMENT,
    SCHEMA_OBJECT_BLOB,
    TAR_BLOCK_SIZE,
    TAR_EOF_SIZE,
    align_up,
    atomic_json,
    closure_store_fsync_enabled,
    fail,
    hash_file,
    maybe_failpoint,
    physical_store_path,
    read_json,
    remove_path,
    require_tool,
    run_checked,
    store_entry,
    tree_hash,
    utc_now,
)
from .segments import (
    create_segment_locked,
    current_open_segment_locked,
    file_size,
    object_blob_meta_path,
    object_extent_from_meta,
    object_meta_path,
    read_segment_meta,
    seal_segment_locked,
    segment_blob_path,
    segment_file_size,
    segment_lock_path,
    tar_eof_meta_path,
    write_segment_meta,
)


def create_tar_fragment(source, entry, target):
    source = Path(source)
    target = Path(target)
    full_tar = target.with_suffix(".full.tar")
    if target.exists():
        target.unlink()
    if full_tar.exists():
        full_tar.unlink()

    tar = require_tool("tar")
    run_checked(
        [
            tar,
            "--format=posix",
            # POSIX/PAX stores atime and ctime even with --mtime. These change
            # when Nix restores/deduplicates a package, breaking cached seed
            # descriptors. Keep long-path support without host inode metadata
            # or process-dependent extended-header names.
            "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime",
            "--hard-dereference",
            "--sort=name",
            "--numeric-owner",
            "--owner=0",
            "--group=0",
            "--mtime=@1",
            "--blocking-factor=1",
            "-cf",
            str(full_tar),
            "-C",
            str(source.parent),
            "--",
            source.name,
        ]
    )

    size = file_size(full_tar)
    if size < TAR_EOF_SIZE:
        fail(f"tar fragment is too small for {entry}")
    with full_tar.open("r+b") as handle:
        handle.seek(size - TAR_EOF_SIZE)
        trailer = handle.read(TAR_EOF_SIZE)
        if trailer != b"\0" * TAR_EOF_SIZE:
            fail(f"tar fragment does not end with a standard EOF marker for {entry}")
        handle.truncate(size - TAR_EOF_SIZE)
    if file_size(full_tar) % TAR_BLOCK_SIZE != 0:
        fail(f"tar fragment is not tar-block aligned for {entry}")
    os.replace(full_tar, target)
    os.chmod(target, 0o400)


def append_file_to_open_segment_blob(backing, source, segment_id, txid):
    source = Path(source)
    digest = hash_file(source)
    size = file_size(source)
    if size % TAR_BLOCK_SIZE != 0:
        fail(f"object data is not tar-block aligned: {source}")

    with source.open("rb") as data:
        backing.seek(0, os.SEEK_END)
        offset = align_up(backing.tell(), OBJECT_BLOB_ALIGNMENT)
        padding = offset - backing.tell()
        if padding:
            backing.write(b"\0" * padding)
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            backing.write(chunk)
        backing.flush()
        if closure_store_fsync_enabled():
            os.fsync(backing.fileno())

    return {
        "segment_id": segment_id,
        "segment_offset": offset,
        "size": size,
        "sha256": digest,
        "transaction": txid,
    }


def append_file_to_segment(root, source, entry, txid, target_bytes):
    root = Path(root)
    source = Path(source)
    size = file_size(source)
    lock_path = segment_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        oversized = size > target_bytes
        if oversized:
            segment_id, meta = create_segment_locked(
                root, target_bytes, txid, oversized=True, make_current=False
            )
        else:
            current = current_open_segment_locked(root)
            if current is None:
                segment_id, meta = create_segment_locked(root, target_bytes, txid)
            else:
                segment_id, meta = current
            blob_size = segment_file_size(root, segment_id)
            offset = align_up(blob_size, OBJECT_BLOB_ALIGNMENT)
            if int(meta.get("object_count", 0)) > 0 and offset + size > target_bytes:
                seal_segment_locked(root, segment_id, txid, "target-size-rollover")
                segment_id, meta = create_segment_locked(root, target_bytes, txid)

        blob_path = segment_blob_path(root, segment_id)
        with blob_path.open("r+b") as backing:
            result = append_file_to_open_segment_blob(backing, source, segment_id, txid)
        result["segment_blob"] = str(blob_path)

        meta = read_segment_meta(root, segment_id)
        used_bytes = int(result["segment_offset"]) + int(result["size"])
        objects = list(meta.get("objects", []))
        objects.append(
            {
                "entry": entry,
                "offset": int(result["segment_offset"]),
                "size": int(result["size"]),
                "sha256": result["sha256"],
            }
        )
        meta.update(
            {
                "updated_at": utc_now(),
                "used_bytes": max(int(meta.get("used_bytes", 0)), used_bytes),
                "object_count": int(meta.get("object_count", 0)) + 1,
                "objects": objects,
            }
        )
        write_segment_meta(root, segment_id, meta)
        if oversized:
            seal_segment_locked(root, segment_id, txid, "oversized-object")
        elif int(meta["used_bytes"]) >= target_bytes:
            seal_segment_locked(root, segment_id, txid, "target-size-reached")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return result


def ensure_tar_eof_extent(root, txid, target_bytes):
    root = Path(root)
    meta_path = tar_eof_meta_path(root)
    if meta_path.exists():
        meta = read_json(meta_path)
        object_extent_from_meta(root, meta, "canonical tar EOF")
        return meta

    tmp = root / "tmp" / f"tar-eof-{txid}"
    tmp.write_bytes(b"\0" * TAR_EOF_SIZE)
    os.chmod(tmp, 0o400)
    extent = append_file_to_segment(root, tmp, ".tar-eof", txid, target_bytes)
    remove_path(tmp)
    payload = {
        "schema": "airlock.closure-store.tar-eof.v1",
        "segment_id": extent["segment_id"],
        "segment_blob": extent["segment_blob"],
        "segment_offset": extent["segment_offset"],
        "size": extent["size"],
        "sha256": extent["sha256"],
        "admitted_at": utc_now(),
        "transaction": txid,
    }
    atomic_json(meta_path, payload)
    return payload


def admit_object_tar_fragment(
    root, logical_path, source, source_hash, txid, target_bytes
):
    root = Path(root)
    entry = store_entry(logical_path)
    meta = object_blob_meta_path(root, entry)
    if meta.exists():
        current = read_json(meta)
        if current.get("tree_hash") != source_hash:
            fail(f"existing object hash mismatch for {logical_path}")
        object_extent_from_meta(root, current, f"canonical object {entry}")
        return dict(current, admitted=False)

    tmp = root / "tmp" / f"object-tar-{entry}-{txid}"
    if tmp.exists():
        remove_path(tmp)
    create_tar_fragment(source, entry, tmp)

    lock_path = segment_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if meta.exists():
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            remove_path(tmp)
            current = read_json(meta)
            if current.get("tree_hash") != source_hash:
                fail(f"existing object hash mismatch for {logical_path}")
            object_extent_from_meta(root, current, f"canonical object {entry}")
            return dict(current, admitted=False)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    result = admit_prebuilt_tar_fragment(
        root, logical_path, tmp, source_hash, txid, target_bytes
    )
    remove_path(tmp)
    return result


def admit_prebuilt_tar_fragment(
    root, logical_path, fragment, source_hash, txid, target_bytes
):
    """Publish an already validated deterministic object tar fragment."""
    root = Path(root)
    fragment = Path(fragment)
    entry = store_entry(logical_path)
    meta = object_blob_meta_path(root, entry)
    if meta.exists():
        current = read_json(meta)
        if current.get("tree_hash") != source_hash:
            fail(f"existing object hash mismatch for {logical_path}")
        object_extent_from_meta(root, current, f"canonical object {entry}")
        return dict(current, admitted=False)
    if file_size(fragment) % TAR_BLOCK_SIZE != 0:
        fail(f"object data is not tar-block aligned: {logical_path}")

    extent = append_file_to_segment(root, fragment, entry, txid, target_bytes)
    lock_path = segment_lock_path(root)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if meta.exists():
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            current = read_json(meta)
            if current.get("tree_hash") != source_hash:
                fail(f"existing object hash mismatch for {logical_path}")
            object_extent_from_meta(root, current, f"canonical object {entry}")
            return dict(current, admitted=False)
        maybe_failpoint("object-after-append")
        payload = {
            "schema": SCHEMA_OBJECT_BLOB,
            "path": logical_path,
            "entry": entry,
            "tree_hash": source_hash,
            "format": "posix-tar-fragment-v1",
            "segment_id": extent["segment_id"],
            "segment_blob": extent["segment_blob"],
            "segment_offset": extent["segment_offset"],
            "size": extent["size"],
            "sha256": extent["sha256"],
            "admitted_at": utc_now(),
            "transaction": txid,
            "locations": [
                {
                    "segment_id": extent["segment_id"],
                    "segment_offset": int(extent["segment_offset"]),
                    "size": int(extent["size"]),
                    "sha256": extent["sha256"],
                    "status": "current",
                    "created_at": utc_now(),
                    "transaction": txid,
                }
            ],
        }
        atomic_json(meta, payload)
        maybe_failpoint("object-after-metadata")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    payload["admitted"] = True
    return payload


def admit_prebuilt_object(
    root, logical_path, fragment, source_hash, txid, target_bytes
):
    entry = store_entry(logical_path)
    existing_meta_path = object_meta_path(root, entry)
    existing = read_json(existing_meta_path) if existing_meta_path.exists() else {}
    if existing and existing.get("tree_hash") != source_hash:
        fail(f"existing object hash mismatch for {logical_path}")
    tar_fragment = admit_prebuilt_tar_fragment(
        root, logical_path, fragment, source_hash, txid, target_bytes
    )
    admitted = not existing or tar_fragment.get("admitted", False)
    payload = {
        "schema": "airlock.closure-store.object.v2",
        "path": logical_path,
        "entry": entry,
        "tree_hash": source_hash,
        "tar_fragment": {
            "schema": SCHEMA_OBJECT_BLOB,
            "segment_id": tar_fragment["segment_id"],
            "segment_blob": tar_fragment["segment_blob"],
            "segment_offset": int(tar_fragment["segment_offset"]),
            "size": int(tar_fragment["size"]),
            "sha256": tar_fragment["sha256"],
            "format": tar_fragment.get("format", "posix-tar-fragment-v1"),
            "locations": tar_fragment.get("locations", []),
        },
        "tree_materialized": False,
        "admitted_at": existing.get("admitted_at", utc_now()),
    }
    atomic_json(existing_meta_path, payload)
    return {
        "path": logical_path,
        "entry": entry,
        "tree_hash": source_hash,
        "tar_fragment": payload["tar_fragment"],
        "admitted": admitted,
    }


def reusable_object(root, logical_path):
    """Trust complete admission records in the dom0-owned append-only pool.

    Incomplete publication after interruption must be verified/admitted again.
    Payload integrity scrubbing remains a separate, explicit operation.
    """
    entry = store_entry(logical_path)
    meta = object_meta_path(root, entry)
    blob_meta = object_blob_meta_path(root, entry)
    if not meta.exists() or not blob_meta.exists():
        return None
    obj, blob = read_json(meta), read_json(blob_meta)
    if obj.get("schema") != "airlock.closure-store.object.v2":
        return None
    if any(
        record.get("path") != logical_path or record.get("entry") != entry
        for record in (obj, blob)
    ):
        fail(f"object identity mismatch for {logical_path}")
    if not obj.get("tree_hash") or obj["tree_hash"] != blob.get("tree_hash"):
        fail(f"object admission metadata mismatch for {logical_path}")
    extent = object_extent_from_meta(root, blob, logical_path, verify_payload=False)
    recorded = object_extent_from_meta(
        root, obj["tar_fragment"], logical_path, verify_payload=False
    )
    if extent != recorded:
        fail(f"object extent metadata mismatch for {logical_path}")
    return dict(obj, admitted=False)


def admit_object(root, logical_path, physical_store_root, txid, target_bytes):
    entry = store_entry(logical_path)
    source = physical_store_path(logical_path, physical_store_root)
    if not source.exists() and not source.is_symlink():
        fail(f"store object source is missing: {logical_path} ({source})")

    source_hash = tree_hash(source)
    meta = object_meta_path(root, entry)
    existing_meta = read_json(meta) if meta.exists() else {}
    if existing_meta and existing_meta.get("tree_hash") != source_hash:
        fail(f"existing object hash mismatch for {logical_path}")

    tar_fragment = admit_object_tar_fragment(
        root, logical_path, source, source_hash, txid, target_bytes
    )
    admitted = not existing_meta or tar_fragment.get("admitted", False)
    payload = {
        "schema": "airlock.closure-store.object.v2",
        "path": logical_path,
        "entry": entry,
        "tree_hash": source_hash,
        "tar_fragment": {
            "schema": SCHEMA_OBJECT_BLOB,
            "segment_id": tar_fragment["segment_id"],
            "segment_blob": tar_fragment["segment_blob"],
            "segment_offset": int(tar_fragment["segment_offset"]),
            "size": int(tar_fragment["size"]),
            "sha256": tar_fragment["sha256"],
            "format": tar_fragment.get("format", "posix-tar-fragment-v1"),
            "locations": tar_fragment.get(
                "locations",
                [
                    {
                        "segment_id": tar_fragment["segment_id"],
                        "segment_offset": int(tar_fragment["segment_offset"]),
                        "size": int(tar_fragment["size"]),
                        "sha256": tar_fragment["sha256"],
                        "status": "current",
                        "created_at": tar_fragment.get("admitted_at", utc_now()),
                        "transaction": tar_fragment.get("transaction"),
                    }
                ],
            ),
        },
        "tree_materialized": False,
        "admitted_at": existing_meta.get("admitted_at", utc_now()),
    }
    atomic_json(meta, payload)

    return {
        "path": logical_path,
        "entry": entry,
        "tree_hash": source_hash,
        "tar_fragment": payload["tar_fragment"],
        "admitted": admitted,
    }
