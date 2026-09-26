"""Host publication into the existing pool, with boot records committed last.

No Qubes imports: the same storage primitives run in image construction and
initramfs. Callers hold admission_lock across publication/selection/pruning.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote

from . import builder, core, state, views
from .pool import admission_lock

POOL = "/var/lib/airlock/closure-store"
HOST = "/var/lib/airlock/host-store"
SCHEMA = "airlock.host-store.v1"


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


def overlay_uri():
    lower = f"local?root={HOST}/lower&read-only=true"
    return f"local-overlay://?lower-store={quote(lower, safe='')}&upper-layer={HOST}/upper"


def key(system):
    return core.store_entry(system)


def admit(root, domain, system, registration, *, incremental=True):
    from .cli import build_parser
    from .generations import admit_generation

    paths = builder.registration_paths(registration)
    if system not in paths:
        core.fail("system missing from host/builder registration")
    with tempfile.TemporaryDirectory(dir=Path(root) / "tmp") as temporary:
        source = Path(temporary)
        core.atomic_write(source / "store-paths", "\n".join(sorted(paths)) + "\n")
        core.atomic_json(source / "manifest.json", {})
        args = build_parser().parse_args([
            "admit", "--pool-root", str(root), "--domain", domain,
            "--system", system, "--closure-source", str(source),
            "--approval-source", "host-publication", "--approved-by", "dom0",
            "--verify-mode", "incremental" if incremental else "tree",
            "--activate", "false",
        ])
        args.physical_store_root = "/nix/store"
        result = admit_generation(args)
    directory = Path(result["manifest"]).parent
    core.atomic_write(directory / "registration", registration, mode=0o444)
    return result


def publish(root, system, registration, systems=None, *, incremental=True):
    """Admission, durable metadata and physical retention precede boot visibility."""
    root = Path(root)
    existing = root / "host/boots" / (key(system) + ".json")
    if existing.exists():
        return validate_boot(root, system)
    result = admit(root, "dom0", system, registration, incremental=incremental)
    manifest_path = Path(result["manifest"])
    manifest = core.read_json(manifest_path)
    paths = builder.registration_paths(registration)
    systems = systems or [system]
    if not set(systems) <= paths:
        core.fail("host boot choices are not in admitted closure")
    store = manifest["store"]
    record = {
        "schema": SCHEMA, "generation": manifest["generation"],
        "manifest": str(manifest_path),
        "registration": str(manifest_path.parent / "registration"),
        "registration_sha256": hashlib.sha256(registration.encode()).hexdigest(),
        "metadata_image": store["canonical_metadata_image"],
        "metadata_sha256": core.hash_file(store["canonical_metadata_image"]),
        "blob_map": store["canonical_blob_map"],
        "blob_map_sha256": core.hash_file(store["canonical_blob_map"]),
        "closure_hash": store["closure_hash"],
    }
    # The pin protects the canonical physical extents from relocation as well as
    # generation GC. It is released only by explicit host retirement.
    state.mark_running_ref(root, "dom0", manifest["generation"], "host-retained")
    # Flush only this generation's storage, not unrelated VM writable disks.
    blob_map = core.read_json(record["blob_map"])
    durable = list(views.segment_path_map_from_blob_map(blob_map).values()) + [
        record["registration"], record["metadata_image"], record["blob_map"]
    ]
    for path in durable:
        with open(path, "rb") as stream:
            os.fsync(stream.fileno())
    core.maybe_failpoint("host-before-boot-record")
    for choice in systems:
        core.atomic_json(root / "host/boots" / (key(choice) + ".json"), dict(record, system=choice))
    return dict(record, system=system)


def validate_boot(root, system, *, logical_root=None, allow_retired=False):
    root = Path(root)
    logical_root = Path(logical_root or root)
    def physical(path):
        path = Path(path)
        if not path.is_relative_to(logical_root) or ".." in path.parts:
            core.fail("host metadata escapes pool")
        return root / path.relative_to(logical_root)
    record = core.read_json(root / "host/boots" / (key(system) + ".json"))
    if record.get("schema") != SCHEMA or record.get("system") != system:
        core.fail("host boot record identity mismatch")
    if record.get("retired") and not allow_retired:
        core.fail("host boot generation has been retired")
    manifest = core.read_json(physical(record["manifest"]))
    if not state.generation_is_committed(root, manifest) or manifest["domain"] != "dom0":
        core.fail("host generation is not committed")
    if (manifest["generation"] != record["generation"] or
            manifest["store"]["closure_hash"] != record["closure_hash"] or
            manifest["store"]["canonical_metadata_image"] != record["metadata_image"] or
            manifest["store"]["canonical_blob_map"] != record["blob_map"] or
            system not in manifest["store"]["paths"]):
        core.fail("host boot record differs from pool generation")
    for field in ("registration", "metadata_image", "blob_map"):
        path = physical(record[field])
        if path.is_symlink():
            core.fail("host metadata escapes pool")
        if core.hash_file(path) != record[field + "_sha256"]:
            core.fail(f"host {field} checksum mismatch")
    if builder.registration_paths(physical(record["registration"]).read_text()) != set(manifest["store"]["paths"]):
        core.fail("host registration differs from pool view")
    pin = state.running_ref_path(root, "dom0", record["generation"], "host-retained")
    if not pin.exists():
        core.fail("host retention pin missing")
    pinned = core.read_json(pin)
    expected = views.blob_map_physical_references(
        core.read_json(physical(record["blob_map"])),
        f"running:dom0:{record['generation']}:host-retained",
    )
    if (pinned.get("schema") != core.SCHEMA_RUNNING_REF or pinned.get("status") != "running" or
            pinned.get("generation") != record["generation"] or pinned.get("domain") != "dom0" or
            pinned.get("manifest") != record["manifest"] or
            pinned.get("closure_hash") != record["closure_hash"] or
            pinned.get("physical_references") != [ref for group in expected.values() for ref in group]):
        core.fail("host retention pin is inconsistent")
    return record


def refresh_builder(root, role, host_registration, *, incremental=True):
    """Publish an additive successor; Qubes must attach it only while stopped."""
    root = Path(root)
    previous = core.read_json(root / f"builders/{role}.json")
    directory = state.generation_manifest_path(root, f"{role}-builder", previous["generation"]).parent
    registration = builder.merge_registration((directory / "registration").read_text(), host_registration)
    if hashlib.sha256(registration.encode()).hexdigest() == previous["registration_sha256"]:
        return previous
    old = core.read_json(directory / "manifest.json")
    result = admit(root, f"{role}-builder", old["system"], registration, incremental=incremental)
    builder.prepare_builder(root, result, Path(result["manifest"]).parent / "registration",
                            directory / "flake-inputs.json", predecessor=previous)
    return core.read_json(root / f"builders/{role}.json")


def publish_live(system):
    # Temporary Nix root prevents ordinary Nix GC racing with verification and
    # admission. Pool serialization is independent of the Nix GC lock.
    root = Path(POOL)
    with admission_lock(root):
        if (root / "host/boots" / (key(system) + ".json")).exists():
            record = validate_boot(root, system)
            refresh_builder(root, "sys-ctrl", Path(record["registration"]).read_text())
            return record
    temporary = Path(HOST) / "publishing"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary) as directory:
        command("nix-store", "--add-root", directory + "/root", "--realise", system)
        paths = command("nix-store", "--query", "--requisites", system).splitlines()
        registration = command("nix-store", "--store", overlay_uri(), "--dump-db", *paths)
        with admission_lock(root):
            record = publish(root, system, registration)
            refresh_builder(root, "sys-ctrl", registration)
            return record


def select(system):
    with admission_lock(POOL):
        record = validate_boot(POOL, system)
        from ..host_artifacts import select_entry, verify
        entry = verify(POOL, "/boot", record)
        select_entry("/boot", entry)
        core.atomic_json(Path(POOL) / "host/selected.json", record)
        return record


def collect_retired(root):
    """Release retired host views only after the last offline upper transition."""
    root = Path(root)
    records = [(path, core.read_json(path)) for path in (root / "host/boots").glob("*.json")]
    for generation in {record["generation"] for _, record in records}:
        group = [(path, record) for path, record in records if record["generation"] == generation]
        if not all(record.get("retired") for _, record in group):
            continue
        refs = root / f"running/dom0/{generation}"
        if any(path.name != "host-retained.json" for path in refs.glob("*.json")):
            continue
        manifest_path = state.generation_manifest_path(root, "dom0", generation)
        manifest = core.read_json(manifest_path)
        manifest["retention"]["retain"] = False
        core.atomic_json(manifest_path, manifest)
        for path, _ in group:
            path.unlink()
        core.fsync_dir(root / "host/boots")
        state.clear_running_ref(root, "dom0", generation, "host-retained")


def retire(system):
    with admission_lock(POOL):
        root = Path(POOL)
        for name in ("selected", "running"):
            pointer = root / f"host/{name}.json"
            if pointer.exists() and core.read_json(pointer)["system"] == system:
                core.fail(f"cannot retire {name} host")
        path = root / "host/boots" / (key(system) + ".json")
        if not path.exists():
            return {"system": system, "retired": True}
        record = validate_boot(root, system, allow_retired=True)
        core.atomic_json(path, dict(record, retired=True))
        collect_retired(root)
        return {"system": system, "retired": True}


def allocated(path):
    """Count allocated bytes, de-duplicating hard links and never following links."""
    seen = set()
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            st = (Path(directory) / name).lstat()
            identity = st.st_dev, st.st_ino
            if identity not in seen:
                seen.add(identity)
                total += st.st_blocks * 512
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("publish", "select", "seal-boot", "retire", "status", "recover"))
    parser.add_argument("system", nargs="?")
    args = parser.parse_args()
    if args.action in ("publish", "select", "retire", "recover") and not args.system:
        parser.error("system is required")
    if args.action == "publish":
        result = publish_live(args.system)
    elif args.action == "select":
        result = select(args.system)
    elif args.action == "seal-boot":
        from ..host_artifacts import seal
        with admission_lock(POOL):
            seal(POOL, "/boot")
        result = {"sealed": True}
    elif args.action == "retire":
        result = retire(args.system)
    elif args.action == "recover":
        # Recovery uses boot artifacts already copied onto ESP. It never mounts
        # a suspect main view over its own running conventional Nix store.
        if not Path("/etc/airlock/pool-recovery").exists():
            core.fail("recover is available only in independent recovery")
        result = select(args.system)
    else:
        result = {"schema": "airlock.host-store.status.v1",
                  "pool_allocated_bytes": allocated(POOL),
                  "upper_allocated_bytes": allocated(Path(HOST) / "upper"),
                  "recovery_allocated_bytes": allocated("/var/lib/airlock/recovery-store")}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
