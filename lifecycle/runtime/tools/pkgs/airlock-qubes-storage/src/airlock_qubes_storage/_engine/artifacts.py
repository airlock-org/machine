"""Portable guest-closure artifacts for direct admission into the shared pool."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .core import (
    SCHEMA_ADMISSION,
    SCHEMA_GENERATION,
    VIEW_BACKEND_EROFS_OBJECTMAP,
    atomic_json,
    closure_hash,
    fail,
    hash_file,
    maybe_failpoint,
    next_generation,
    physical_store_path,
    read_store_paths,
    safe_domain_name,
    store_entry,
    transaction_id,
    tree_hash,
    utc_now,
)
from .objects import admit_prebuilt_object, create_tar_fragment, reusable_object
from .pool import admission_lock, ensure_pool
from .segments import configured_segment_target_bytes
from .state import current_generation
from .views import materialize_view

SCHEMA_ARTIFACT = "airlock.pool-artifact.v1"
SCHEMA_INCREMENTAL = "airlock.pool-artifact.v2"
SCHEMA_REQUEST = "airlock.pool-artifact-request.v1"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
STORE_PATH_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/]+$")
REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
LOCK_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_stream(stream, target=None, limit=None):
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(chunk)
        if limit is not None and size > limit:
            fail("artifact member exceeds its declared size")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return size, digest.hexdigest()


def _normalize_path_info(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {item["path"]: item for item in payload}
    fail("Nix path metadata must be an object or list")


def query_path_info(paths, nix="nix", store=None):
    command = [nix]
    if store:
        command += ["--store", store]
    command += ["path-info", "--json", *paths]
    result = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        fail(
            "cannot read Nix path metadata: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    try:
        return _normalize_path_info(json.loads(result.stdout))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"invalid Nix path metadata: {exc}")


def _validate_store_path(path):
    if not isinstance(path, str) or not STORE_PATH_RE.fullmatch(path):
        fail(f"invalid Nix store path in artifact: {path!r}")
    return path


def _validate_provenance(source_revision, lock_hash):
    if not isinstance(source_revision, str) or not REVISION_RE.fullmatch(
        source_revision
    ):
        fail("artifact source revision must be a committed hexadecimal revision")
    if not isinstance(lock_hash, str) or not LOCK_HASH_RE.fullmatch(lock_hash):
        fail("artifact lock hash must be a 64-character hexadecimal SHA-256")


def write_artifact(
    output,
    *,
    domain,
    system,
    closure_source,
    root_source,
    kernel_name,
    source_revision,
    lock_hash,
    physical_store_root="/nix/store",
    nix="nix",
    nix_store=None,
    path_info=None,
    negotiate=None,
):
    """Write a streaming tar artifact; package fragments precede its manifest."""
    domain = safe_domain_name(domain)
    system = _validate_store_path(system)
    _validate_provenance(source_revision, lock_hash)
    logical_closure_source = str(closure_source)
    closure_path = Path(logical_closure_source)
    if logical_closure_source.startswith("/nix/store/"):
        closure_path = physical_store_path(logical_closure_source, physical_store_root)
    paths = read_store_paths(closure_path, system)
    paths = sorted({_validate_store_path(path) for path in paths})
    metadata = (
        _normalize_path_info(path_info)
        if path_info is not None
        else query_path_info(paths, nix=nix, store=nix_store)
    )
    if set(metadata) != set(paths):
        missing = sorted(set(paths) - set(metadata))
        extra = sorted(set(metadata) - set(paths))
        fail(f"Nix path metadata differs from closure: missing={missing} extra={extra}")

    objects = []
    for logical_path in paths:
        source = physical_store_path(logical_path, physical_store_root)
        info = metadata[logical_path]
        objects.append(
            {
                "path": logical_path,
                "entry": store_entry(logical_path),
                "references": info.get("references", []),
                "nar_hash": info.get("narHash", ""),
                "nar_size": int(info.get("narSize", 0)),
                "tree_hash": tree_hash(source),
                "fragment": f"objects/{store_entry(logical_path)}.tar",
            }
        )
    manifest = {
        "schema": SCHEMA_INCREMENTAL if negotiate else SCHEMA_ARTIFACT,
        "domain": domain,
        "system": system,
        "closure_source": logical_closure_source,
        "root_source": str(root_source),
        "kernel_name": str(kernel_name),
        "source_revision": source_revision,
        "lock_hash": lock_hash,
        "object_count": len(objects),
        "objects": objects,
    }
    _validate_received(manifest, {}, domain, inventory=True)
    missing = set(paths)
    if negotiate:
        request = negotiate(manifest)
        requested = request.get("missing") if isinstance(request, dict) else None
        if (
            not isinstance(request, dict)
            or request.get("schema") != SCHEMA_REQUEST
            or not isinstance(requested, list)
            or any(not isinstance(path, str) for path in requested)
            or len(set(requested)) != len(requested)
            or not set(requested) <= set(paths)
        ):
            fail("invalid missing-object request")
        missing = set(requested)
    with tempfile.TemporaryDirectory(prefix="airlock-pool-artifact-") as temp_name:
        temp = Path(temp_name)
        with tarfile.open(
            fileobj=output, mode="w|", format=tarfile.PAX_FORMAT
        ) as archive:
            for record in objects:
                logical_path = record["path"]
                if logical_path not in missing:
                    continue
                entry = store_entry(logical_path)
                source = physical_store_path(logical_path, physical_store_root)
                if not source.exists() and not source.is_symlink():
                    fail(f"store object source is missing: {logical_path}")
                fragment = temp / f"{entry}.tar"
                create_tar_fragment(source, entry, fragment)
                fragment_size = fragment.stat().st_size
                fragment_hash = hash_file(fragment)
                member_name = f"objects/{entry}.tar"
                member = tarfile.TarInfo(member_name)
                member.size = fragment_size
                member.mode = 0o400
                member.mtime = 1
                with fragment.open("rb") as stream:
                    archive.addfile(member, stream)
                record.update(
                    {
                        "fragment_size": fragment_size,
                        "fragment_sha256": fragment_hash,
                    }
                )
                fragment.unlink()

            payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
            member = tarfile.TarInfo("manifest.json")
            member.size = len(payload)
            member.mode = 0o400
            member.mtime = 1
            archive.addfile(member, io.BytesIO(payload))
    return manifest


def _tar_tree_hash(fragment, expected_entry):
    """Validate a pool tar fragment and reproduce core.tree_hash from it."""
    records = []
    seen = set()
    root = None
    try:
        archive = tarfile.open(fragment, mode="r:")
    except (tarfile.TarError, OSError) as exc:
        fail(f"invalid object tar fragment for {expected_entry}: {exc}")
    with archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                fail(f"unsafe object tar path: {member.name}")
            if path.parts[0] != expected_entry:
                fail(f"object tar path does not match {expected_entry}: {member.name}")
            normalized = path.as_posix().rstrip("/")
            if normalized in seen:
                fail(f"duplicate object tar path: {normalized}")
            seen.add(normalized)
            relative = "/".join(path.parts[1:])
            if not relative:
                if root is not None:
                    fail(f"duplicate object tar root: {expected_entry}")
                root = member
                continue
            if member.isdir():
                records.append((relative, "D", member.mode & 0o7777, None))
            elif member.issym():
                records.append((relative, "L", None, member.linkname))
            elif member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    fail(f"cannot read object tar member: {member.name}")
                _, digest = _sha256_stream(stream)
                records.append((relative, "F", member.mode & 0o7777, digest))
            else:
                fail(f"unsupported object tar member type: {member.name}")
    if root is None:
        fail(f"object tar fragment has no root: {expected_entry}")
    digest = hashlib.sha256()
    if root.issym():
        if records:
            fail(f"symlink object tar root has children: {expected_entry}")
        digest.update(b"L\0")
        digest.update(root.linkname.encode())
    elif root.isfile():
        if records:
            fail(f"file object tar root has children: {expected_entry}")
        with tarfile.open(fragment, mode="r:") as archive:
            stream = archive.extractfile(archive.getmember(root.name))
            _, payload_hash = _sha256_stream(stream)
        digest.update(f"F\0{root.mode & 0o7777:o}\0".encode())
        digest.update(payload_hash.encode())
    elif root.isdir():
        digest.update(b"D\0")
        for relative, kind, mode, value in sorted(records):
            if kind == "L":
                digest.update(f"L\0{relative}\0{value}\0".encode())
            elif kind == "D":
                digest.update(f"D\0{relative}\0{mode:o}\0".encode())
            else:
                digest.update(f"F\0{relative}\0{mode:o}\0".encode())
                digest.update(value.encode())
                digest.update(b"\0")
    else:
        fail(f"unsupported object tar root type: {expected_entry}")
    return digest.hexdigest()


def _receive_members(stream, staging, allowed_fragments=None):
    fragments = {}
    manifest = None
    try:
        archive = tarfile.open(fileobj=stream, mode="r|*")
        with archive:
            for member in archive:
                if not member.isfile():
                    fail(f"artifact member is not a regular file: {member.name}")
                if member.name == "manifest.json":
                    if manifest is not None or member.size > MAX_MANIFEST_BYTES:
                        fail("invalid or duplicate artifact manifest")
                    payload = archive.extractfile(member)
                    try:
                        manifest = json.loads(payload.read(MAX_MANIFEST_BYTES + 1))
                    except (AttributeError, json.JSONDecodeError) as exc:
                        fail(f"invalid artifact manifest JSON: {exc}")
                    continue
                match = re.fullmatch(r"objects/([0-9a-z]{32}-[^/]+)\.tar", member.name)
                if not match or member.name in fragments:
                    fail(f"unexpected or duplicate artifact member: {member.name}")
                if (
                    allowed_fragments is not None
                    and member.name not in allowed_fragments
                ):
                    fail(f"unrequested artifact fragment: {member.name}")
                target = staging / (match.group(1) + ".tar")
                source = archive.extractfile(member)
                with target.open("xb") as output:
                    size, digest = _sha256_stream(source, output, member.size)
                    output.flush()
                    os.fsync(output.fileno())
                if size != member.size:
                    fail(f"truncated artifact member: {member.name}")
                os.chmod(target, 0o400)
                fragments[member.name] = {
                    "path": target,
                    "size": size,
                    "sha256": digest,
                }
    except (tarfile.TarError, OSError) as exc:
        fail(f"cannot read pool artifact: {exc}")
    if manifest is None:
        fail("pool artifact is missing manifest.json")
    return manifest, fragments


def _validate_received(
    manifest, fragments, expected_domain, *, reused=None, inventory=False
):
    if not isinstance(manifest, dict) or manifest.get("schema") not in (
        SCHEMA_ARTIFACT,
        SCHEMA_INCREMENTAL,
    ):
        fail("unsupported pool artifact schema")
    reused = reused or {}
    domain = safe_domain_name(manifest.get("domain"))
    if domain != expected_domain:
        fail(f"pool artifact target mismatch: {domain} != {expected_domain}")
    system = _validate_store_path(manifest.get("system"))
    _validate_provenance(manifest.get("source_revision"), manifest.get("lock_hash"))
    raw_objects = manifest.get("objects")
    if not isinstance(raw_objects, list) or manifest.get("object_count") != len(
        raw_objects
    ):
        fail("pool artifact object count mismatch")
    objects = {}
    for item in raw_objects:
        if not isinstance(item, dict):
            fail("invalid pool artifact object record")
        path = _validate_store_path(item.get("path"))
        entry = store_entry(path)
        if item.get("entry") != entry:
            fail(f"pool artifact object identity mismatch: {path}")
        member_name = f"objects/{entry}.tar"
        if item.get("fragment") != member_name:
            fail(f"pool artifact fragment is missing: {path}")
        if path in objects:
            fail(f"duplicate pool artifact object: {path}")
        if not isinstance(item.get("tree_hash"), str) or not LOCK_HASH_RE.fullmatch(
            item["tree_hash"]
        ):
            fail(f"invalid object tree hash: {path}")
        if not inventory:
            if path in reused:
                if item["tree_hash"] != reused[path]["tree_hash"]:
                    fail(f"existing object hash mismatch for {path}")
            else:
                if member_name not in fragments:
                    fail(f"pool artifact fragment is missing: {path}")
                fragment = fragments[member_name]
                if (
                    item.get("fragment_size") != fragment["size"]
                    or item.get("fragment_sha256") != fragment["sha256"]
                ):
                    fail(f"pool artifact fragment checksum mismatch: {path}")
                if item["tree_hash"] != _tar_tree_hash(fragment["path"], entry):
                    fail(f"pool artifact tree checksum mismatch: {path}")
        references = item.get("references")
        if not isinstance(references, list):
            fail(f"pool artifact references are invalid: {path}")
        if not isinstance(item.get("nar_hash"), str) or not item["nar_hash"]:
            fail(f"pool artifact NAR hash is invalid: {path}")
        if not isinstance(item.get("nar_size"), int) or item["nar_size"] < 0:
            fail(f"pool artifact NAR size is invalid: {path}")
        item["references"] = sorted({_validate_store_path(ref) for ref in references})
        objects[path] = item
    if not inventory and set(fragments) != {
        item["fragment"] for path, item in objects.items() if path not in reused
    }:
        fail("pool artifact contains unlisted object fragments")
    if system not in objects:
        fail("pool artifact does not contain its guest system")
    paths = set(objects)
    for path, item in objects.items():
        missing = sorted(set(item["references"]) - paths)
        if missing:
            fail(f"pool artifact closure is incomplete for {path}: {missing}")
    return domain, system, objects


def _admit_received(manifest, fragments, root, approved_by, *, reused=None):
    root = ensure_pool(root)
    domain, system, objects = _validate_received(
        manifest, fragments, manifest["domain"], reused=reused
    )
    domain_dir = root / "domains" / domain
    generations_dir = domain_dir / "generations"
    generations_dir.mkdir(parents=True, exist_ok=True)
    previous = current_generation(root, domain)
    generation = next_generation(domain_dir)
    txid = transaction_id(f"artifact:{domain}:{generation}:{system}")
    tx_path = root / "transactions" / f"{txid}.json"
    generation_dir = generations_dir / str(generation)
    generation_dir.mkdir(parents=True, exist_ok=False)
    atomic_json(
        tx_path,
        {
            "schema": "airlock.closure-store.transaction.v1",
            "id": txid,
            "domain": domain,
            "generation": generation,
            "status": "preparing",
            "kind": "transferred-artifact",
            "created_at": utc_now(),
        },
    )
    try:
        target_bytes = configured_segment_target_bytes("")
        admitted = []
        reused = []
        for logical_path in sorted(objects):
            record = objects[logical_path]
            existing = reusable_object(root, logical_path)
            if existing is not None:
                if existing.get("tree_hash") != record["tree_hash"]:
                    fail(f"existing object hash mismatch for {logical_path}")
                result = existing
            else:
                result = admit_prebuilt_object(
                    root,
                    logical_path,
                    fragments[record["fragment"]]["path"],
                    record["tree_hash"],
                    txid,
                    target_bytes,
                )
            (admitted if result.get("admitted") else reused).append(result)
            maybe_failpoint("artifact-after-object-import")
        paths = sorted(objects)
        closure_id = closure_hash(
            paths,
            system,
            manifest.get("root_source", ""),
            manifest.get("kernel_name", ""),
        )
        view = materialize_view(
            root,
            domain,
            generation,
            paths,
            closure_id,
            VIEW_BACKEND_EROFS_OBJECTMAP,
            txid,
            target_bytes,
            trusted_paths=set(paths),
        )
        generation_manifest = {
            "schema": SCHEMA_GENERATION,
            "status": "prepared",
            "domain": domain,
            "generation": generation,
            "previous_generation": previous,
            "created_at": utc_now(),
            "transaction": txid,
            "approval": {
                "approved_by": approved_by,
                "source": manifest["closure_source"],
                "transport": "qrexec",
            },
            "provenance": {
                "source_revision": manifest["source_revision"],
                "lock_hash": manifest["lock_hash"],
                "artifact_schema": SCHEMA_ARTIFACT,
            },
            "system": system,
            "root": {"source": manifest.get("root_source", "")},
            "kernel": {"name": manifest.get("kernel_name", "")},
            "store": {
                "mode": "shared-closure",
                "closure_hash": closure_id,
                "source": manifest["closure_source"],
                "mountpoint": "/nix/store",
                "device": "/dev/xvde",
                "blob_device": "/dev/xvdf",
                "blob_volume": view["blob_volume"],
                "read_only": True,
                "filesystem": view["filesystem"],
                "view_backend": view["view_backend"],
                "paths": paths,
                "object_count": len(paths),
                "view_dir": view["view_dir"],
                "image": view["image"],
                "canonical_image": view["canonical_image"],
                "metadata_image": view["metadata_image"],
                "canonical_metadata_image": view["canonical_metadata_image"],
                "blob_map": view["blob_map"],
                "canonical_blob_map": view["canonical_blob_map"],
                "object_refs": view.get("object_refs"),
                "canonical_object_refs": view.get("canonical_object_refs"),
                "blob_logical_size": view["blob_logical_size"],
                "object_data_bytes": view.get("object_data_bytes", 0),
                "object_extent_count": view.get("object_extent_count", 0),
                "segment_count": view.get("segment_count", 0),
                "segments": view.get("segments", []),
                "image_contains_materialized_store_copy": view[
                    "image_contains_materialized_store_copy"
                ],
                "segment_target_bytes": target_bytes,
            },
            "verification": {
                "mode": "transferred-artifact",
                "status": "passed",
                "verified_objects": len(paths),
            },
            "retention": {"retain": True},
        }
        manifest_path = generation_dir / "manifest.json"
        maybe_failpoint("artifact-before-generation-commit")
        atomic_json(manifest_path, generation_manifest)
        atomic_json(
            tx_path,
            {
                "schema": "airlock.closure-store.transaction.v1",
                "id": txid,
                "domain": domain,
                "generation": generation,
                "status": "committed",
                "activated": False,
                "committed_at": utc_now(),
                "manifest": str(manifest_path),
            },
        )
        generation_manifest["status"] = "committed"
        atomic_json(manifest_path, generation_manifest)
    except Exception:
        atomic_json(
            tx_path,
            {
                "schema": "airlock.closure-store.transaction.v1",
                "id": txid,
                "domain": domain,
                "generation": generation,
                "status": "aborted",
                "aborted_at": utc_now(),
            },
        )
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    return {
        "schema": SCHEMA_ADMISSION,
        "status": "passed",
        "transport": "qrexec",
        "domain": domain,
        "generation": generation,
        "previous_generation": previous,
        "current_generation": current_generation(root, domain),
        "activated": False,
        "retained": True,
        "manifest": str(manifest_path),
        "system": system,
        "closure_hash": closure_id,
        "objects_total": len(paths),
        "objects_admitted": len(admitted),
        "objects_reused": len(reused),
        "objects_transferred": len(fragments),
        "payload_bytes_transferred": sum(
            fragment["size"] for fragment in fragments.values()
        ),
        "source_revision": manifest["source_revision"],
        "lock_hash": manifest["lock_hash"],
    }


def receive_artifact(stream, *, pool_root, expected_domain, approved_by):
    expected_domain = safe_domain_name(expected_domain)
    root = ensure_pool(pool_root)
    staging = Path(tempfile.mkdtemp(prefix="incoming-", dir=root / "tmp"))
    try:
        manifest, fragments = _receive_members(stream, staging)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA_ARTIFACT:
            fail("incremental artifacts require a negotiated session")
        domain, _, _ = _validate_received(manifest, fragments, expected_domain)
        if domain != expected_domain:
            fail("pool artifact target mismatch")
        with admission_lock(root):
            return _admit_received(manifest, fragments, root, approved_by)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def write_message(stream, payload):
    data = json.dumps(payload, sort_keys=True).encode() + b"\n"
    if len(data) > MAX_MANIFEST_BYTES:
        fail("pool protocol message is too large")
    stream.write(data)
    stream.flush()


def read_message(stream):
    data = stream.readline(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES or not data.endswith(b"\n"):
        fail("missing, truncated or oversized pool protocol message")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        fail("invalid pool protocol JSON")


def receive_incremental(stream, output, *, pool_root, expected_domain, approved_by):
    """Keep reusable objects protected from GC until generation publication."""
    expected_domain = safe_domain_name(expected_domain)
    inventory = read_message(stream)
    if not isinstance(inventory, dict) or inventory.get("schema") != SCHEMA_INCREMENTAL:
        fail("unsupported incremental inventory schema")
    _, _, objects = _validate_received(inventory, {}, expected_domain, inventory=True)
    root = ensure_pool(pool_root)
    with admission_lock(root):
        reused = {}
        for path, record in objects.items():
            existing = reusable_object(root, path)
            if existing is not None:
                if existing["tree_hash"] != record["tree_hash"]:
                    fail(f"existing object hash mismatch for {path}")
                reused[path] = existing
        write_message(
            output,
            {
                "schema": SCHEMA_REQUEST,
                "missing": sorted(set(objects) - set(reused)),
            },
        )
        staging = Path(tempfile.mkdtemp(prefix="incoming-", dir=root / "tmp"))
        try:
            manifest, fragments = _receive_members(
                stream,
                staging,
                {
                    record["fragment"]
                    for path, record in objects.items()
                    if path not in reused
                },
            )
            _validate_received(manifest, fragments, expected_domain, reused=reused)
            # Only transport checksums may be added after negotiation.
            comparable = dict(manifest)
            comparable["objects"] = [
                {
                    key: value
                    for key, value in record.items()
                    if key not in ("fragment_size", "fragment_sha256")
                }
                for record in manifest.get("objects", [])
            ]
            if comparable != inventory:
                fail("artifact differs from negotiated inventory")
            return _admit_received(
                manifest, fragments, root, approved_by, reused=reused
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
