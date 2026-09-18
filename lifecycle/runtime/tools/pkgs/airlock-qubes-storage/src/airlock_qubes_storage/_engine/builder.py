"""Image-seeded, frozen sys-dev build inputs; no guest control authority."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from . import core, state, views


def registration_paths(text):
    """Check the closureInfo registration framing and reference completeness."""
    lines = iter(text.splitlines())
    paths = set()
    references = set()
    try:
        while (path := next(lines, None)) is not None:
            core.store_entry(path)
            if path in paths:
                raise ValueError("duplicate registration path")
            paths.add(path)
            nar_hash = next(lines)
            if not re.fullmatch(
                r"(?:[0-9a-f]{64}|sha256:[0-9abcdfghijklmnpqrsvwxyz]{52})", nar_hash
            ):
                raise ValueError("invalid registration hash")
            if int(next(lines)) < 0:
                raise ValueError("invalid NAR size")
            next(lines)  # Deriver is informational and may not be present.
            count = int(next(lines))
            if count < 0:
                raise ValueError("invalid reference count")
            for _ in range(count):
                reference = next(lines)
                core.store_entry(reference)
                references.add(reference)
    except (StopIteration, ValueError) as exc:
        core.fail(f"invalid builder registration: {exc}")
    if not paths or not references <= paths:
        core.fail("builder registration is not reference-complete")
    return paths


def prepare_builder(root, admission, registration):
    """Build metadata only; package bytes are shared with guest pool views."""
    root = Path(root)
    manifest_path = Path(admission["manifest"])
    manifest = core.read_json(manifest_path)
    if manifest["domain"] != "sys-dev-builder":
        core.fail("builder preparation is restricted to sys-dev-builder")
    registration = Path(registration).read_bytes()
    if registration_paths(registration.decode()) != set(manifest["store"]["paths"]):
        core.fail("builder registration does not match frozen view")
    identity = {
        "schema": "airlock.sys-dev-builder.v1",
        "domain": manifest["domain"],
        "generation": manifest["generation"],
        "closure_hash": manifest["store"]["closure_hash"],
        "registration_sha256": hashlib.sha256(registration).hexdigest(),
    }
    metadata_image = manifest_path.parent / "builder-metadata.erofs"
    with tempfile.TemporaryDirectory(dir=root / "tmp", prefix="builder-") as temporary:
        source = Path(temporary)
        source.chmod(0o755)
        (source / "registration").write_bytes(registration)
        core.atomic_json(source / "identity.json", identity)
        (source / "skeleton/.links").mkdir(parents=True)
        views.run_checked(
            [
                views.require_tool("mkfs.erofs", "AIRLOCK_MKFS_EROFS"),
                "--quiet",
                "--all-root",
                "-T",
                "1",
                "-U",
                "00000000-0000-0000-0000-000000000001",
                str(metadata_image),
                str(source),
            ]
        )
    os.chmod(metadata_image, 0o444)
    manifest["builder"] = {**identity, "metadata_image": str(metadata_image)}
    core.atomic_json(manifest_path, manifest)
    # This is intentionally NOT tied to VM running state. Persistent upper
    # outputs can reference lower objects while the VM is off, or after an OS
    # update. Only an explicit offline disposal of that upper can release it.
    state.mark_running_ref(
        root, manifest["domain"], manifest["generation"], "builder-persistent"
    )
    core.atomic_json(root / "builders/sys-dev.json", manifest["builder"])


def builder_volume_configs(root, pool_name):
    """Validated boot volumes for the single image-seeded sys-dev builder."""
    root = Path(root)
    descriptor = root / "builders/sys-dev.json"
    if not descriptor.exists():
        return {}
    data = core.read_json(descriptor)
    if (
        data.get("schema") != "airlock.sys-dev-builder.v1"
        or data.get("domain") != "sys-dev-builder"
    ):
        core.fail("invalid sys-dev builder descriptor")
    generation = int(data["generation"])
    manifest_path = state.generation_manifest_path(root, "sys-dev-builder", generation)
    manifest = core.read_json(manifest_path)
    if (
        not state.generation_is_committed(root, manifest)
        or manifest.get("builder") != data
    ):
        core.fail("sys-dev builder descriptor does not match committed generation")
    pin = core.read_json(
        state.running_ref_path(
            root, "sys-dev-builder", generation, "builder-persistent"
        )
    )
    blob_map = core.read_json(manifest["store"]["canonical_blob_map"])
    expected_refs = [
        ref
        for refs in views.blob_map_physical_references(
            blob_map, f"running:sys-dev-builder:{generation}:builder-persistent"
        ).values()
        for ref in refs
    ]
    expected_pin = {
        "schema": core.SCHEMA_RUNNING_REF,
        "status": "running",
        "domain": "sys-dev-builder",
        "generation": generation,
        "volume": "builder-persistent",
        "manifest": str(manifest_path),
        "closure_hash": manifest["store"]["closure_hash"],
        "physical_references": expected_refs,
    }
    if any(pin.get(key) != value for key, value in expected_pin.items()):
        core.fail("sys-dev builder persistent pin is missing or inconsistent")
    store = manifest["store"]
    common = {
        "pool": pool_name,
        "rw": False,
        "snap_on_start": False,
        "save_on_stop": True,
        "source": None,
        "revisions_to_keep": 0,
        "domain_name": "sys-dev-builder",
        "generation": str(generation),
        "closure_hash": store["closure_hash"],
        "manifest_path": str(manifest_path),
    }
    images = {
        "builder-store": store["canonical_metadata_image"],
        "builder-metadata": data["metadata_image"],
    }
    result = {}
    for name in ("builder-store", "builder-data", "builder-metadata"):
        volume = {
            **common,
            "name": name,
            "vid": f"domains/sys-dev-builder/generations/{generation}/{name}",
        }
        if name == "builder-data":
            volume.update(
                view_kind="erofs-blob-map",
                blob_map_path=store["canonical_blob_map"],
                blob_logical_size=store["blob_logical_size"],
                size=store["blob_logical_size"],
            )
        else:
            path = Path(images[name])
            if path.stat().st_mode & 0o222:
                core.fail("builder metadata image must be read-only")
            volume.update(
                view_kind="metadata-image",
                view_path=str(path),
                size=path.stat().st_size,
            )
        result[name] = volume
    return result
