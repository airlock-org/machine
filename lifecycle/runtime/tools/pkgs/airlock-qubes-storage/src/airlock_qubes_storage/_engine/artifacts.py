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
import time
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
    read_json,
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
from .state import current_generation, generation_is_committed, generation_manifest_path
from .views import materialize_view, view_integrity, verify_reusable_view
from ..root_format import encode, validate_descriptor
from .root_images import install_root, reusable_root, source_artifact, verify_generation_root

SCHEMA_ARTIFACT = "airlock.pool-artifact.full.v2"
SCHEMA_INCREMENTAL = "airlock.pool-artifact.v4"
SCHEMA_PROBE = "airlock.pool-generation-probe.v1"
SCHEMA_REQUEST = "airlock.pool-artifact-request.v2"
SCHEMA_ERROR = "airlock.pool-artifact-error.v1"
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
    root_path, root_artifact = (source_artifact(root_source, physical_store_root)
                               if root_source else (None, None))
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
                **({} if negotiate else {"tree_hash": tree_hash(source)}),
                "fragment": f"objects/{store_entry(logical_path)}.tar",
            }
        )
    manifest = {
        "schema": SCHEMA_INCREMENTAL if negotiate else SCHEMA_ARTIFACT,
        "domain": domain,
        "system": system,
        "closure_source": logical_closure_source,
        "root_source": str(root_source),
        "root_artifact": root_artifact,
        "kernel_name": str(kernel_name),
        "source_revision": source_revision,
        "lock_hash": lock_hash,
        "object_count": len(objects),
        "objects": objects,
    }
    _validate_received(manifest, {}, domain, inventory=True)
    missing = set(paths)
    missing_root = root_artifact is not None
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
            or type(request.get("missing_root")) is not bool
            or (request["missing_root"] and root_artifact is None)
        ):
            fail("invalid missing-object request")
        missing = set(requested)
        missing_root = request["missing_root"]
        reused_hashes = request.get("reused_tree_hashes", {})
        if (not isinstance(reused_hashes, dict)
                or set(reused_hashes) != set(paths) - missing
                or any(not isinstance(value, str) or not LOCK_HASH_RE.fullmatch(value)
                       for value in reused_hashes.values())):
            fail("invalid reused-object identities")
        # The pool owns already admitted bytes. Only additions require reading
        # the builder's filesystem; immutable shared packages are never rehashed.
        for record in objects:
            path = record["path"]
            record["tree_hash"] = (tree_hash(physical_store_path(path, physical_store_root))
                                   if path in missing else reused_hashes[path])
    with tempfile.TemporaryDirectory(prefix="airlock-pool-artifact-") as temp_name:
        temp = Path(temp_name)
        with tarfile.open(
            fileobj=output, mode="w|", format=tarfile.PAX_FORMAT
        ) as archive:
            if missing_root:
                root_payload = temp / "root.sparse"
                with root_payload.open("xb") as stream:
                    if encode(root_path, stream) != root_artifact:
                        fail("root image changed during transfer")
                member = tarfile.TarInfo("root.sparse")
                member.size = root_artifact["payload_bytes"]
                member.mode = 0o400
                member.mtime = 1
                with root_payload.open("rb") as stream:
                    archive.addfile(member, stream)
                root_payload.unlink()
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
        # Match tree_hash's Path ordering, including e.g. pam/environment
        # before pam.d. Sorting flattened strings changes existing identities.
        for relative, kind, mode, value in sorted(
            records, key=lambda record: PurePosixPath(record[0])
        ):
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


def _receive_members(stream, staging, allowed_fragments=None, root_descriptor=None):
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
                is_root = member.name == "root.sparse"
                if (not match and not is_root) or member.name in fragments:
                    fail(f"unexpected or duplicate artifact member: {member.name}")
                if (
                    allowed_fragments is not None
                    and member.name not in allowed_fragments
                ):
                    fail(f"unrequested artifact fragment: {member.name}")
                if is_root:
                    # Bound allocation before consuming a root payload.
                    from ..root_format import MAX_SIZE
                    limit = root_descriptor["payload_bytes"] if root_descriptor else MAX_SIZE
                    if member.size > limit:
                        fail("root artifact exceeds negotiated size")
                target = staging / ("root.sparse" if is_root else match.group(1) + ".tar")
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
    manifest, fragments, expected_domain, *, reused=None, inventory=False, root_reused=False
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
    root_artifact = manifest.get("root_artifact")
    if root_artifact is not None:
        validate_descriptor(root_artifact)
        if not manifest.get("root_source"):
            fail("root artifact has no source identity")
        if not inventory and not root_reused:
            fragment = fragments.get("root.sparse", {})
            if (fragment.get("size") != root_artifact["payload_bytes"]
                    or fragment.get("sha256") != root_artifact["sha256"]):
                fail("root artifact payload checksum mismatch")
    elif manifest.get("root_source"):
        fail("root source has no artifact descriptor")
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
        if not (inventory and manifest["schema"] == SCHEMA_INCREMENTAL and "tree_hash" not in item) and (
            not isinstance(item.get("tree_hash"), str) or not LOCK_HASH_RE.fullmatch(item["tree_hash"])
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
    expected_fragments = {
        item["fragment"] for path, item in objects.items() if path not in reused
    }
    if root_artifact is not None and not root_reused:
        expected_fragments.add("root.sparse")
    if not inventory and set(fragments) != expected_fragments:
        fail("pool artifact contains unlisted object fragments")
    if system not in objects:
        fail("pool artifact does not contain its guest system")
    paths = set(objects)
    for path, item in objects.items():
        missing = sorted(set(item["references"]) - paths)
        if missing:
            fail(f"pool artifact closure is incomplete for {path}: {missing}")
    return domain, system, objects


def _admit_received(manifest, fragments, root, approved_by, *, reused=None, root_reused=False):
    root = ensure_pool(root)
    validation_started = time.monotonic()
    domain, system, objects = _validate_received(
        manifest, fragments, manifest["domain"], reused=reused, root_reused=root_reused
    )
    payload_validation_seconds = time.monotonic() - validation_started
    root_started = time.monotonic()
    domain_dir = root / "domains" / domain
    generations_dir = domain_dir / "generations"
    generations_dir.mkdir(parents=True, exist_ok=True)
    previous = current_generation(root, domain)
    root_source = manifest.get("root_source", "")
    kernel_name = manifest.get("kernel_name", "")
    root_artifact = manifest.get("root_artifact")
    if not root_source or not kernel_name:
        if previous is None:
            fail(
                "system-only pool admission requires an existing generation "
                "from which to inherit root and kernel identity"
            )
        previous_manifest = read_json(generation_manifest_path(root, domain, previous))
        if not generation_is_committed(root, previous_manifest):
            fail(f"current generation is not committed for {domain}: {previous}")
        root_source = root_source or previous_manifest.get("root", {}).get("source", "")
        kernel_name = kernel_name or previous_manifest.get("kernel", {}).get("name", "")
        if root_artifact is None:
            verify_generation_root(root, previous_manifest)
            root_artifact = previous_manifest["root"]["artifact"]
    if not root_source or not kernel_name:
        fail("pool admission requires complete root and kernel identity")
    root_fragment = fragments.get("root.sparse")
    if root_fragment:
        root_image = install_root(root, root_fragment["path"], root_artifact)
    else:
        root_image = reusable_root(root, root_artifact)
        if root_image is None:
            fail("negotiated root artifact is missing")
    package_fragments = {key: value for key, value in fragments.items() if key != "root.sparse"}
    root_stats = {
        "root_artifact": root_artifact,
        "root_reused": root_fragment is None,
        "root_payload_bytes_transferred": root_fragment["size"] if root_fragment else 0,
        "root_logical_bytes": root_artifact["logical_bytes"],
        "root_allocated_bytes": root_image.stat().st_blocks * 512,
    }
    admission_started = time.monotonic()
    timings = {"payload_validation_seconds": payload_validation_seconds,
               "root_admission_seconds": admission_started - root_started}
    # Retry a candidate without creating another retained view. Reuse only an
    # exact committed, verified generation with matching admission provenance.
    # Incremental negotiation has already checked the payload identities.
    for existing_path in sorted(generations_dir.glob("*/manifest.json")):
        existing = read_json(existing_path)
        if (not existing.get("store", {}).get("integrity")
                or not generation_is_committed(root, existing)
                or existing.get("system") != system
                or existing.get("root", {}).get("source") != root_source
                or existing.get("root", {}).get("artifact") != root_artifact
                or existing.get("kernel", {}).get("name") != kernel_name
                or existing.get("store", {}).get("paths") != sorted(objects)
                or existing.get("approval") != {
                    "approved_by": approved_by, "source": manifest["closure_source"],
                    "transport": "qrexec"}
                or existing.get("provenance", {}).get("source_revision") != manifest["source_revision"]
                or existing.get("provenance", {}).get("lock_hash") != manifest["lock_hash"]
                or existing.get("retention", {}).get("retain") is not True
                or existing.get("verification", {}).get("status") != "passed"):
            continue
        if any((saved := reusable_object(root, path)) is None
               or saved.get("tree_hash") != obj["tree_hash"]
               for path, obj in objects.items()):
            continue
        return {
            "schema": SCHEMA_ADMISSION, "status": "passed", "transport": "qrexec",
            "domain": domain, "generation": existing["generation"],
            "previous_generation": existing.get("previous_generation"),
            "current_generation": previous, "activated": False, "retained": True,
            "manifest": str(existing_path), "system": system, "root_source": root_source,
            "kernel_name": kernel_name, "closure_hash": existing["store"]["closure_hash"],
            "objects_total": len(objects), "objects_admitted": 0,
            "objects_reused": len(objects), "objects_transferred": len(package_fragments),
            "payload_bytes_transferred": sum(fragment["size"] for fragment in package_fragments.values()),
            **root_stats,
            "timings": {**timings, "package_admission_seconds": time.monotonic() - admission_started},
            "source_revision": manifest["source_revision"], "lock_hash": manifest["lock_hash"],
        }
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
            root_source,
            kernel_name,
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
                "artifact_schema": manifest["schema"],
            },
            "system": system,
            "root": {"source": root_source, "artifact": root_artifact},
            "kernel": {"name": kernel_name},
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
                "integrity": view_integrity(view),
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
        "root_source": root_source,
        "kernel_name": kernel_name,
        "closure_hash": closure_id,
        "objects_total": len(paths),
        "objects_admitted": len(admitted),
        "objects_reused": len(reused),
        "objects_transferred": len(package_fragments),
        "payload_bytes_transferred": sum(
            fragment["size"] for fragment in package_fragments.values()
        ),
        **root_stats,
        "timings": {**timings, "package_admission_seconds": time.monotonic() - admission_started},
        "source_revision": manifest["source_revision"],
        "lock_hash": manifest["lock_hash"],
    }


def receive_artifact(stream, *, pool_root, expected_domain, approved_by):
    expected_domain = safe_domain_name(expected_domain)
    root = ensure_pool(pool_root)
    with admission_lock(root):
        staging = Path(tempfile.mkdtemp(prefix="incoming-", dir=root / "tmp"))
        try:
            manifest, fragments = _receive_members(stream, staging)
            if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA_ARTIFACT:
                fail("incremental artifacts require a negotiated session")
            if manifest.get("domain") != expected_domain:
                fail("pool artifact target mismatch")
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


def reuse_generation(root, probe, expected_domain, approved_by):
    """Reuse an exact immutable closure; keep its original admission provenance."""
    if probe.get("domain") != expected_domain:
        fail("pool artifact target mismatch")
    _validate_store_path(probe.get("system"))
    _validate_provenance(probe.get("source_revision"), probe.get("lock_hash"))
    for path in sorted((root / "domains" / expected_domain / "generations").glob("*/manifest.json"),
                       key=lambda p: int(p.parent.name), reverse=True):
        manifest = read_json(path)
        store = manifest.get("store", {})
        if (not store.get("integrity")
                or not generation_is_committed(root, manifest)
                or manifest.get("system") != probe["system"]
                or store.get("source") != probe.get("closure_source")
                or manifest.get("root", {}).get("source") != probe.get("root_source")
                or manifest.get("kernel", {}).get("name") != probe.get("kernel_name")
                or manifest.get("approval") != {"approved_by": approved_by,
                    "source": probe.get("closure_source"), "transport": "qrexec"}
                or manifest.get("verification", {}).get("status") != "passed"
                or manifest.get("retention", {}).get("retain") is not True):
            continue
        # Check committed metadata and backing availability without rereading
        # every package. Explicit pool scrubbing remains responsible for bit rot.
        verify_reusable_view(root, manifest)
        if any(reusable_object(root, item) is None for item in store["paths"]):
            fail("retained guest object is missing")
        verify_generation_root(root, manifest)
        artifact = manifest["root"]["artifact"]
        image = reusable_root(root, artifact)
        return {
            "schema": SCHEMA_ADMISSION, "status": "passed", "transport": "qrexec",
            "domain": expected_domain, "generation": manifest["generation"],
            "previous_generation": manifest.get("previous_generation"),
            "current_generation": current_generation(root, expected_domain),
            "activated": False, "retained": True, "manifest": str(path),
            "system": manifest["system"], "root_source": probe["root_source"],
            "kernel_name": probe["kernel_name"], "closure_hash": store["closure_hash"],
            "objects_total": len(store["paths"]), "objects_reused": len(store["paths"]),
            "objects_admitted": 0, "objects_transferred": 0, "payload_bytes_transferred": 0,
            "root_artifact": artifact, "root_reused": True,
            "root_payload_bytes_transferred": 0, "root_logical_bytes": artifact["logical_bytes"],
            "root_allocated_bytes": image.stat().st_blocks * 512,
            "source_revision": manifest["provenance"]["source_revision"],
            "lock_hash": manifest["provenance"]["lock_hash"], "generation_reused": True,
        }
    return None


def receive_incremental(stream, output, *, pool_root, expected_domain, approved_by):
    """Keep reusable objects protected from GC until generation publication."""
    expected_domain = safe_domain_name(expected_domain)
    inventory = read_message(stream)
    if isinstance(inventory, dict) and inventory.get("schema") == SCHEMA_PROBE:
        root = ensure_pool(pool_root)
        started = time.monotonic()
        with admission_lock(root):
            receipt = reuse_generation(root, inventory, expected_domain, approved_by)
            if receipt is not None:
                receipt["timings"] = {"generation_reuse_seconds": time.monotonic() - started}
                return receipt
        write_message(output, {"schema": SCHEMA_PROBE, "reuse": False})
        inventory = read_message(stream)
    if not isinstance(inventory, dict) or inventory.get("schema") != SCHEMA_INCREMENTAL:
        fail("unsupported incremental inventory schema")
    _, _, objects = _validate_received(inventory, {}, expected_domain, inventory=True)
    root = ensure_pool(pool_root)
    with admission_lock(root):
        started = time.monotonic()
        # No other admission can own these directories while this lock is held.
        # Remove fragments abandoned by process death before starting a retry.
        for orphan in list((root / "tmp").glob("incoming-*")) + list((root / "tmp").glob("root-incoming-*")):
            if orphan.is_dir() and not orphan.is_symlink():
                shutil.rmtree(orphan)
        reused = {}
        for path, record in objects.items():
            existing = reusable_object(root, path)
            if existing is not None:
                if "tree_hash" in record and existing["tree_hash"] != record["tree_hash"]:
                    fail(f"existing object hash mismatch for {path}")
                reused[path] = existing
        root_descriptor = inventory.get("root_artifact")
        root_reused = root_descriptor is not None and reusable_root(root, root_descriptor) is not None
        if root_descriptor is not None and not root_reused:
            available = shutil.disk_usage(root).free
            # Retain the wire payload while creating a sparse verified image.
            if available < 2 * root_descriptor["payload_bytes"] + 64 * 1024**2:
                fail("insufficient pool space for root artifact admission")
        validation_seconds = time.monotonic() - started
        write_message(
            output,
            {
                "schema": SCHEMA_REQUEST,
                "missing": sorted(set(objects) - set(reused)),
                "missing_root": root_descriptor is not None and not root_reused,
                "reused_tree_hashes": {path: obj["tree_hash"] for path, obj in reused.items()},
            },
        )
        staging = Path(tempfile.mkdtemp(prefix="incoming-", dir=root / "tmp"))
        try:
            started = time.monotonic()
            manifest, fragments = _receive_members(
                stream,
                staging,
                {
                    record["fragment"]
                    for path, record in objects.items()
                    if path not in reused
                } | ({"root.sparse"} if root_descriptor is not None and not root_reused else set()),
                root_descriptor,
            )
            transfer_seconds = time.monotonic() - started
            started = time.monotonic()
            _validate_received(manifest, {}, expected_domain, inventory=True)
            # Only transport checksums may be added after negotiation.
            comparable = dict(manifest)
            comparable["objects"] = [
                {
                    key: value
                    for key, value in record.items()
                    if key not in ("fragment_size", "fragment_sha256")
                    and (key != "tree_hash" or "tree_hash" in objects.get(record["path"], {}))
                }
                for record in manifest.get("objects", [])
            ]
            if comparable != inventory:
                fail("artifact differs from negotiated inventory")
            result = _admit_received(
                manifest, fragments, root, approved_by, reused=reused, root_reused=root_reused
            )
            result["timings"].update(inventory_validation_seconds=validation_seconds,
                                     payload_receive_seconds=transfer_seconds,
                                     validation_and_admission_seconds=time.monotonic() - started)
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)
