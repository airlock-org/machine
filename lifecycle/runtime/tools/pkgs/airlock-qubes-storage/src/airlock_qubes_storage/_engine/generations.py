import os


from .core import (
    SCHEMA_ADMISSION,
    SCHEMA_GENERATION,
    SCHEMA_GENERATION_RETIREMENT,
    SCHEMA_RETENTION,
    SCHEMA_ROLLBACK,
    SCHEMA_SWITCH,
    VIEW_BACKEND_EROFS_OBJECTMAP,
    atomic_json,
    closure_hash,
    fail,
    maybe_failpoint,
    next_generation,
    parse_bool,
    read_json,
    read_store_paths,
    run_verify,
    safe_domain_name,
    transaction_id,
    utc_now,
)
from .objects import admit_object, reusable_object
from .pool import ensure_pool
from .segments import configured_segment_target_bytes
from .state import (
    clear_current,
    clear_running_ref,
    current_generation,
    generation_is_committed,
    generation_manifest_path,
    iter_running_generation_refs,
    mark_running_ref,
    switch_current,
)
from .views import materialize_view


def mark_running(args):
    return mark_running_ref(args.pool_root, args.domain, args.generation, args.volume)


def clear_running(args):
    return clear_running_ref(args.pool_root, args.domain, args.generation, args.volume)


def find_generation(args):
    """Find the newest inactive/retained generation matching a release entry."""
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    generations = root / "domains" / domain / "generations"
    matches = []
    if generations.exists():
        for directory in generations.iterdir():
            if not directory.is_dir() or not directory.name.isdigit():
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = read_json(manifest_path)
            store = manifest.get("store", {})
            approval = manifest.get("approval", {})
            if not generation_is_committed(root, manifest):
                continue
            if any(
                [
                    manifest.get("domain") != domain,
                    manifest.get("system") != args.system,
                    manifest.get("root", {}).get("source", "") != args.root_source,
                    manifest.get("kernel", {}).get("name", "") != args.kernel_name,
                    store.get("source", "") != args.closure_source,
                    store.get("view_backend") != args.view_backend,
                    approval.get("approved_by") != args.approved_by,
                ]
            ):
                continue
            matches.append((int(directory.name), manifest_path, manifest))
    if not matches:
        return {
            "schema": "airlock.closure-store.generation-match.v1",
            "status": "missing",
            "domain": domain,
        }
    generation, manifest_path, manifest = max(matches, key=lambda item: item[0])
    return {
        "schema": "airlock.closure-store.generation-match.v1",
        "status": "found",
        "domain": domain,
        "generation": generation,
        "manifest": str(manifest_path),
        "closure_hash": manifest["store"]["closure_hash"],
    }


def describe_generation(args):
    """Return the activation identity of one committed pool generation."""
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    generation = int(args.generation)
    manifest_path = generation_manifest_path(root, domain, generation)
    if not manifest_path.exists():
        fail(f"generation does not exist for {domain}: {generation}")
    manifest = read_json(manifest_path)
    if not generation_is_committed(root, manifest):
        fail(f"generation is not committed for {domain}: {generation}")
    store = manifest.get("store", {})
    return {
        "schema": "airlock.closure-store.generation-info.v1",
        "status": "passed",
        "domain": domain,
        "generation": generation,
        "current_generation": current_generation(root, domain),
        "selected": current_generation(root, domain) == generation,
        "manifest": str(manifest_path),
        "system": manifest.get("system"),
        "root_source": manifest.get("root", {}).get("source", ""),
        "kernel_name": manifest.get("kernel", {}).get("name", ""),
        "closure_source": store.get("source", ""),
        "store": {
            key: store.get(key)
            for key in (
                "mode",
                "mountpoint",
                "device",
                "blob_device",
                "view_backend",
                "closure_hash",
            )
        },
        "approval": manifest.get("approval", {}),
        "provenance": manifest.get("provenance", {}),
        "verification": manifest.get("verification", {}),
        "retention": manifest.get("retention", {}),
    }


def admit_generation(args):
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    domain_dir = root / "domains" / domain
    generations_dir = domain_dir / "generations"
    generations_dir.mkdir(parents=True, exist_ok=True)
    previous = current_generation(root, domain)
    generation = next_generation(domain_dir)
    txid = transaction_id(f"{domain}:{generation}:{args.system}")
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
            "created_at": utc_now(),
        },
    )
    maybe_failpoint("admit-after-tx-create")

    paths = read_store_paths(args.closure_source, args.system)
    paths = sorted(set(paths))
    if args.system not in paths:
        paths.append(args.system)
        paths = sorted(set(paths))
    target_bytes = configured_segment_target_bytes(args.segment_target_bytes)

    incremental = args.verify_mode == "incremental"
    cached = {}
    if incremental:
        cached = {
            path: obj
            for path in paths
            if (obj := reusable_object(root, path)) is not None
        }
        additions = [path for path in paths if path not in cached]
        verification = run_verify(additions, "nix-store", recursive=False)
        verification.update(
            mode="incremental",
            verified_objects=len(additions),
            reused_objects=len(cached),
        )
    else:
        verification = run_verify([args.system], args.verify_mode)
    admitted = []
    reused = []
    for logical_path in paths:
        result = cached.get(logical_path) or admit_object(
            root, logical_path, args.physical_store_root, txid, target_bytes
        )
        if result["admitted"]:
            admitted.append(result)
        else:
            reused.append(result)

    closure_id = closure_hash(paths, args.system, args.root_source, args.kernel_name)
    view = materialize_view(
        root,
        domain,
        generation,
        paths,
        closure_id,
        args.view_backend,
        txid,
        target_bytes,
        trusted_paths=set(cached),
    )
    manifest_path = generation_dir / "manifest.json"
    manifest = {
        "schema": SCHEMA_GENERATION,
        "status": "prepared",
        "domain": domain,
        "generation": generation,
        "previous_generation": previous,
        "created_at": utc_now(),
        "transaction": txid,
        "approval": {
            "approved_by": args.approved_by,
            "source": args.approval_source,
        },
        "system": args.system,
        "root": {
            "source": args.root_source,
        },
        "kernel": {
            "name": args.kernel_name,
        },
        "store": {
            "mode": "shared-closure",
            "closure_hash": closure_id,
            "source": args.closure_source,
            "mountpoint": args.store_mountpoint,
            "device": args.store_device,
            "blob_device": args.store_blob_device if view["blob_volume"] else None,
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
        "verification": verification,
        "retention": {
            "retain": parse_bool(args.retain),
        },
    }
    maybe_failpoint("generation-before-manifest")
    atomic_json(manifest_path, manifest)
    maybe_failpoint("generation-after-manifest")
    atomic_json(
        tx_path,
        {
            "schema": "airlock.closure-store.transaction.v1",
            "id": txid,
            "domain": domain,
            "generation": generation,
            "status": "committed",
            "activated": parse_bool(args.activate),
            "committed_at": utc_now(),
            "manifest": str(manifest_path),
        },
    )
    maybe_failpoint("generation-after-tx-commit")
    manifest["status"] = "committed"
    atomic_json(manifest_path, manifest)
    maybe_failpoint("generation-after-manifest-commit")
    if parse_bool(args.activate):
        switch_current(root, domain, generation)

    return {
        "schema": SCHEMA_ADMISSION,
        "status": "passed",
        "pool_root": str(root),
        "domain": domain,
        "generation": generation,
        "previous_generation": previous,
        "current_generation": current_generation(root, domain),
        "activated": parse_bool(args.activate),
        "manifest": str(manifest_path),
        "system": args.system,
        "closure_hash": closure_id,
        "verification": verification,
        "store": manifest["store"],
        "objects_total": len(paths),
        "objects_admitted": len(admitted),
        "objects_reused": len(reused),
        "admitted": admitted,
        "reused": reused,
        "view_created": view["image_created"],
        "view_image": view["image"],
        "canonical_view_image": view["canonical_image"],
        "metadata_image": view["metadata_image"],
        "canonical_metadata_image": view["canonical_metadata_image"],
        "blob_map": view["blob_map"],
        "canonical_blob_map": view["canonical_blob_map"],
        "object_refs": view.get("object_refs"),
        "canonical_object_refs": view.get("canonical_object_refs"),
        "blob_device": args.store_blob_device if view["blob_volume"] else None,
        "blob_volume": view["blob_volume"],
        "blob_logical_size": view["blob_logical_size"],
        "object_data_bytes": view.get("object_data_bytes", 0),
        "object_extent_count": view.get("object_extent_count", 0),
        "segment_count": view.get("segment_count", 0),
        "segments": view.get("segments", []),
        "filesystem": view["filesystem"],
        "view_backend": view["view_backend"],
        "view_size": os.path.getsize(view["canonical_image"]),
        "read_only": True,
    }


def rollback(args):
    result = switch(args)
    result["schema"] = SCHEMA_ROLLBACK
    return result


def set_retention(args):
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    generation = int(args.generation)
    manifest_path = generation_manifest_path(root, domain, generation)
    manifest = read_json(manifest_path)
    if not generation_is_committed(root, manifest):
        fail(f"generation is not committed for {domain}: {generation}")
    retain = parse_bool(args.retain)
    retention = manifest.setdefault("retention", {})
    previous = bool(retention.get("retain", False))
    retention["retain"] = retain
    retention["updated_at"] = utc_now()
    atomic_json(manifest_path, manifest)
    return {
        "schema": SCHEMA_RETENTION,
        "status": "passed",
        "pool_root": str(root),
        "domain": domain,
        "generation": generation,
        "previous_retain": previous,
        "retain": retain,
        "manifest": str(manifest_path),
    }


def retire_generation(args):
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    generation = int(args.generation)
    manifest_path = generation_manifest_path(root, domain, generation)
    tombstone = root / "domains" / domain / "retired-generations" / f"{generation}.json"
    prior = read_json(tombstone) if tombstone.exists() else None
    manifest = read_json(manifest_path) if manifest_path.exists() else None
    running = [
        str(path)
        for ref_domain, ref_generation, path, _ in iter_running_generation_refs(root)
        if ref_domain == domain and ref_generation == generation
    ]
    if running:
        fail(f"generation still has running references for {domain}: {generation}")
    if manifest is None:
        if prior and current_generation(root, domain) is None:
            # GC may finish between pool retirement and the coordinator's
            # durable purge completion record. Preserve an idempotent receipt.
            prior["status"] = "retired"
            atomic_json(tombstone, prior)
            return prior
        fail(f"generation is missing for {domain}: {generation}")
    if not generation_is_committed(root, manifest):
        fail(f"generation is not committed for {domain}: {generation}")
    retention = manifest.setdefault("retention", {})
    current = current_generation(root, domain)
    if current != generation and (
        current is not None
        or (
            retention.get("retain", False)
            and (not prior or prior.get("status") != "retiring")
        )
    ):
        fail(f"generation is not current for {domain}: {generation}")
    result = {
        "schema": SCHEMA_GENERATION_RETIREMENT,
        "status": "retiring",
        "pool_root": str(root),
        "domain": domain,
        "generation": generation,
        "manifest": str(manifest_path),
    }
    atomic_json(tombstone, result)
    retention["retain"] = False
    retention["updated_at"] = utc_now()
    retention["retired_at"] = utc_now()
    atomic_json(manifest_path, manifest)
    if current == generation:
        clear_current(root, domain, generation)
    result["status"] = "retired"
    atomic_json(tombstone, result)
    return result


def switch(args):
    root = ensure_pool(args.pool_root)
    domain = safe_domain_name(args.domain)
    generation = int(args.generation)
    manifest = read_json(generation_manifest_path(root, domain, generation))
    if not generation_is_committed(root, manifest):
        fail(f"generation is not committed for {domain}: {generation}")
    previous = current_generation(root, domain)
    switch_current(root, domain, generation)
    return {
        "schema": SCHEMA_SWITCH,
        "status": "passed",
        "pool_root": str(root),
        "domain": domain,
        "previous_generation": previous,
        "current_generation": generation,
        "manifest": str(generation_manifest_path(root, domain, generation)),
        "system": manifest["system"],
        "root_source": manifest.get("root", {}).get("source", ""),
        "kernel_name": manifest.get("kernel", {}).get("name", ""),
        "store": manifest["store"],
        "view_image": manifest["store"]["image"],
        "canonical_view_image": manifest["store"]["canonical_image"],
        "metadata_image": manifest["store"].get(
            "metadata_image", manifest["store"]["image"]
        ),
        "canonical_metadata_image": manifest["store"].get(
            "canonical_metadata_image", manifest["store"]["canonical_image"]
        ),
        "blob_map": manifest["store"].get("blob_map"),
        "canonical_blob_map": manifest["store"].get("canonical_blob_map"),
        "object_refs": manifest["store"].get("object_refs"),
        "canonical_object_refs": manifest["store"].get("canonical_object_refs"),
        "blob_device": manifest["store"].get("blob_device"),
        "blob_volume": manifest["store"].get("blob_volume"),
        "segment_count": manifest["store"].get("segment_count", 0),
        "segments": manifest["store"].get("segments", []),
        "filesystem": manifest["store"].get("filesystem", "erofs"),
        "view_backend": manifest["store"].get(
            "view_backend", VIEW_BACKEND_EROFS_OBJECTMAP
        ),
        "read_only": True,
    }
