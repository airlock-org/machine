"""Approved builder inputs and explicit, offline, additive base transitions."""

import hashlib
import os
import re
import shutil
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


def offline_source_root(source):
    """Return the store object containing a canonical offline source path."""
    match = (
        re.fullmatch(
            r"(/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[^/]+)(/.*)?",
            source,
        )
        if isinstance(source, str)
        else None
    )
    if (
        match is None
        or "\x00" in source
        or any(part in ("", ".", "..") for part in source.split("/")[4:])
    ):
        core.fail(f"invalid offline flake source path: {source!r}")
    return match.group(1)


def registration_records(text):
    """Parse once and retain exact Nix registration records for metadata unions."""
    registration_paths(text)
    lines = text.splitlines()
    result = {}
    offset = 0
    while offset < len(lines):
        end = offset + 5 + int(lines[offset + 4])
        result[lines[offset]] = "\n".join(lines[offset:end]) + "\n"
        offset = end
    return result


def merge_registration(*texts):
    records = {}
    for text in texts:
        for path, record in registration_records(text).items():
            if path in records and records[path].splitlines()[1:3] != record.splitlines()[1:3]:
                core.fail(f"conflicting Nix registration: {path}")
            if path in records and records[path].splitlines()[4:] != record.splitlines()[4:]:
                core.fail(f"conflicting Nix references: {path}")
            records[path] = record
    return "".join(records[path] for path in sorted(records))


def prepare_builder(root, admission, registration, offline_inputs_source=None, predecessor=None):
    """Build metadata only; package bytes are shared with guest pool views."""
    root = Path(root)
    manifest_path = Path(admission["manifest"])
    manifest = core.read_json(manifest_path)
    builder_domain = manifest["domain"]
    if builder_domain not in ("sys-dev-builder", "sys-ctrl-builder"):
        core.fail("builder preparation is restricted to declared builder domains")
    role = builder_domain.removesuffix("-builder")
    registration = Path(registration).read_bytes()
    paths = registration_paths(registration.decode())
    if paths != set(manifest["store"]["paths"]):
        core.fail("builder registration does not match frozen view")
    offline_bytes = None
    if offline_inputs_source is not None and Path(offline_inputs_source).exists():
        offline_bytes = Path(offline_inputs_source).read_bytes()
        offline = core.read_json(offline_inputs_source)
        if (
            offline.get("schema") != "airlock.sys-dev-offline-inputs.v1"
            or not isinstance(offline.get("inputs"), dict)
            or not offline["inputs"]
        ):
            core.fail("offline flake inputs do not match frozen builder view")
        # Local path inputs can live inside another archived source object.
        # The frozen view registers whole store objects, not their subdirectories.
        for name, source in offline["inputs"].items():
            if offline_source_root(source) not in paths:
                core.fail(
                    "offline flake inputs do not match frozen builder view: "
                    f"{name}: {source}"
                )
    identity = {
        "schema": f"airlock.{role}-builder.v1",
        "domain": manifest["domain"],
        "generation": manifest["generation"],
        "closure_hash": manifest["store"]["closure_hash"],
        "registration_sha256": hashlib.sha256(registration).hexdigest(),
    }
    if offline_bytes is not None:
        identity["flake_inputs_sha256"] = hashlib.sha256(offline_bytes).hexdigest()
    if predecessor is not None:
        old_manifest = core.read_json(state.generation_manifest_path(
            root, builder_domain, predecessor["generation"]
        ))
        if old_manifest.get("builder") != predecessor:
            core.fail("builder transition predecessor differs from committed base")
        old_registration = (Path(old_manifest["builder"]["metadata_image"]).parent / "registration").read_text()
        if merge_registration(old_registration, registration.decode()) != merge_registration(registration.decode()):
            core.fail("builder transition must retain every previous registered object")
        identity["predecessors"] = predecessor.get("predecessors", []) + [
            {key: predecessor[key] for key in ("generation", "closure_hash", "registration_sha256")}
        ]
    # Copy only the small index, never the external package payload. Keep the
    # canonical guest index untouched: this image belongs to the frozen builder.
    metadata_image = manifest_path.parent / "builder-store.erofs"
    shutil.copyfile(manifest["store"]["canonical_metadata_image"], metadata_image)
    os.chmod(metadata_image, 0o644)
    with tempfile.TemporaryDirectory(dir=root / "tmp", prefix="builder-") as temporary:
        source = Path(temporary)
        source.chmod(0o755)
        metadata = source / ".airlock-builder"
        metadata.mkdir()
        (metadata / "registration").write_bytes(registration)
        if offline_bytes is not None:
            (metadata / "flake-inputs.json").write_bytes(offline_bytes)
        core.atomic_json(metadata / "identity.json", identity)
        (source / ".links").mkdir()
        views.run_checked(
            [
                views.require_tool("mkfs.erofs", "AIRLOCK_MKFS_EROFS"),
                "--quiet",
                "--incremental=data",
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
    with metadata_image.open("rb") as stream:
        os.fsync(stream.fileno())
    manifest["builder"] = {**identity, "metadata_image": str(metadata_image)}
    core.atomic_write(manifest_path.parent / "registration", registration.decode(), mode=0o444)
    if offline_bytes is not None:
        core.atomic_write(manifest_path.parent / "flake-inputs.json", offline_bytes.decode(), mode=0o444)
    core.atomic_json(manifest_path, manifest)
    # This is intentionally NOT tied to VM running state. Persistent upper
    # outputs can reference lower objects while the VM is off, or after an OS
    # update. Only an explicit offline disposal of that upper can release it.
    state.mark_running_ref(
        root, manifest["domain"], manifest["generation"], "builder-persistent"
    )
    core.atomic_json(root / f"builders/{role}.json", manifest["builder"])


def builder_volume_configs(root, pool_name, *, domain="sys-dev", physical_root=None):
    """Validated boot volumes for an image-seeded builder role."""
    root = Path(root)
    if domain not in ("sys-dev", "sys-ctrl"):
        core.fail("invalid builder domain")
    builder_domain = f"{domain}-builder"

    def physical(path):
        return (
            Path(physical_root) / Path(path).relative_to(root)
            if physical_root is not None
            else Path(path)
        )

    descriptor = root / f"builders/{domain}.json"
    if not physical(descriptor).exists():
        return {}
    data = core.read_json(physical(descriptor))
    if (
        data.get("schema") != f"airlock.{domain}-builder.v1"
        or data.get("domain") != builder_domain
    ):
        core.fail(f"invalid {domain} builder descriptor")
    generation = int(data["generation"])
    manifest_path = state.generation_manifest_path(root, builder_domain, generation)
    manifest = core.read_json(physical(manifest_path))
    if (
        not state.generation_is_committed(physical(root), manifest)
        or manifest.get("builder") != data
    ):
        core.fail(f"{domain} builder descriptor does not match committed generation")
    pin = core.read_json(
        physical(
            state.running_ref_path(
                root, builder_domain, generation, "builder-persistent"
            )
        )
    )
    blob_map = core.read_json(physical(manifest["store"]["canonical_blob_map"]))
    expected_refs = [
        ref
        for refs in views.blob_map_physical_references(
            blob_map, f"running:{builder_domain}:{generation}:builder-persistent"
        ).values()
        for ref in refs
    ]
    expected_pin = {
        "schema": core.SCHEMA_RUNNING_REF,
        "status": "running",
        "domain": builder_domain,
        "generation": generation,
        "volume": "builder-persistent",
        "manifest": str(manifest_path),
        "closure_hash": manifest["store"]["closure_hash"],
        "physical_references": expected_refs,
    }
    if any(pin.get(key) != value for key, value in expected_pin.items()):
        core.fail(f"{domain} builder persistent pin is missing or inconsistent")
    store = manifest["store"]
    common = {
        "pool": pool_name,
        "rw": False,
        "snap_on_start": False,
        "save_on_stop": True,
        "source": None,
        "revisions_to_keep": 0,
        "domain_name": builder_domain,
        "generation": str(generation),
        "closure_hash": store["closure_hash"],
        "manifest_path": str(manifest_path),
    }
    result = {}
    for name in ("builder-store", "builder-data"):
        volume = {
            **common,
            "name": name,
            "vid": f"domains/{builder_domain}/generations/{generation}/{name}",
        }
        if name == "builder-data":
            volume.update(
                view_kind="erofs-blob-map",
                blob_map_path=store["canonical_blob_map"],
                blob_logical_size=store["blob_logical_size"],
                size=store["blob_logical_size"],
            )
        else:
            path = Path(data["metadata_image"])
            if physical(path).stat().st_mode & 0o222:
                core.fail("builder metadata image must be read-only")
            volume.update(
                view_kind="metadata-image",
                view_path=str(path),
                size=physical(path).stat().st_size,
            )
        result[name] = volume
    return result


def release_predecessors(root, role, descriptor):
    """After an atomic stopped-VM attachment, the successor owns all base refs."""
    from .pool import admission_lock

    root = Path(root)
    with admission_lock(root):
        if core.read_json(root / f"builders/{role}.json") != descriptor:
            core.fail("builder descriptor changed during stopped attachment")
        for predecessor in descriptor.get("predecessors", []):
            generation = predecessor["generation"]
            path = state.generation_manifest_path(root, f"{role}-builder", generation)
            if not path.exists():
                continue
            manifest = core.read_json(path)
            if manifest["store"]["closure_hash"] != predecessor["closure_hash"]:
                core.fail("builder predecessor identity mismatch during pin handoff")
            manifest["retention"]["retain"] = False
            core.atomic_json(path, manifest)
            state.clear_running_ref(root, f"{role}-builder", generation, "builder-persistent")
