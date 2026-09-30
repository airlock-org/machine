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
import time
import sys
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
    return f"local-overlay://?lower-store={quote(lower, safe='')}&upper-layer={HOST}/active/upper"


def key(system):
    return core.store_entry(system)


def admit(root, domain, system, registration, *, incremental=True, previous_index=None):
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
        args.previous_index = previous_index
        result = admit_generation(args)
    directory = Path(result["manifest"]).parent
    core.atomic_write(directory / "registration", registration, mode=0o444)
    return result


def publish(root, system, registration, systems=None, *, incremental=True, statistics=None):
    """Admission, durable metadata and physical retention precede boot visibility."""
    root = Path(root)
    existing = root / "host/boots" / (key(system) + ".json")
    if existing.exists():
        return validate_boot(root, system)
    previous_index = None
    running = root / "host/running.json"
    if running.exists():
        previous = validate_boot(root, core.read_json(running)["system"])
        previous_index = dict(core.read_json(previous["manifest"])["store"])
        # Legacy host indexes already have authenticated boot-record hashes.
        # Controller bases without integrity metadata use one full build first.
        previous_index["integrity"] = {
            "metadata_sha256": previous["metadata_image_sha256"],
            "layout_sha256": views.blob_layout_hash(core.read_json(previous["blob_map"])),
        }
    result = admit(root, "dom0", system, registration, incremental=incremental,
                   previous_index=previous_index)
    if statistics is not None:
        statistics.update({"host_" + name: value for name, value in result.get("timings", {}).items()})
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
        "metadata_image_sha256": core.hash_file(store["canonical_metadata_image"]),
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


def refresh_builder(root, role, host_registration, *, incremental=True, statistics=None):
    """Publish an additive successor; Qubes must attach it only while stopped."""
    root = Path(root)
    previous = core.read_json(root / f"builders/{role}.json")
    directory = state.generation_manifest_path(root, f"{role}-builder", previous["generation"]).parent
    registration = builder.merge_registration((directory / "registration").read_text(), host_registration)
    if hashlib.sha256(registration.encode()).hexdigest() == previous["registration_sha256"]:
        return previous
    old = core.read_json(directory / "manifest.json")
    result = admit(root, f"{role}-builder", old["system"], registration, incremental=incremental,
                   previous_index=old["store"])
    if statistics is not None:
        statistics.update({"controller_" + name: value for name, value in result.get("timings", {}).items()})
    builder.prepare_builder(root, result, Path(result["manifest"]).parent / "registration",
                            directory / "flake-inputs.json", predecessor=previous)
    return core.read_json(root / f"builders/{role}.json")


def publish_live(system):
    timings = {}
    started = time.monotonic()
    # Temporary Nix root prevents ordinary Nix GC racing with verification and
    # admission. Pool serialization is independent of the Nix GC lock.
    root = Path(POOL)
    with admission_lock(root):
        if (root / "host/boots" / (key(system) + ".json")).exists():
            record = validate_boot(root, system)
            timings["host_validation_seconds"] = time.monotonic() - started
            started = time.monotonic()
            seed_generation_metadata(record)
            timings["host_database_seconds"] = time.monotonic() - started
            started = time.monotonic()
            refresh_builder(root, "sys-ctrl", Path(record["registration"]).read_text(), statistics=timings)
            timings["controller_base_seconds"] = time.monotonic() - started
            return dict(record, timings=timings)
    temporary = Path(HOST) / "publishing"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary) as directory:
        command("nix-store", "--add-root", directory + "/root", "--realise", system)
        registration = publication_registration(system)
        timings["host_registration_seconds"] = time.monotonic() - started
        with admission_lock(root):
            started = time.monotonic()
            record = publish(root, system, registration, statistics=timings)
            timings["host_pool_publication_seconds"] = time.monotonic() - started
            started = time.monotonic()
            seed_generation_metadata(record)
            timings["host_database_seconds"] = time.monotonic() - started
            started = time.monotonic()
            refresh_builder(root, "sys-ctrl", registration, statistics=timings)
            timings["controller_base_seconds"] = time.monotonic() - started
            return dict(record, timings=timings)


def seed_generation_metadata(record):
    from ..host_state import seed_metadata
    directory = Path(HOST) / "generations" / str(record["generation"])
    # Existing generation state belongs to that generation; never overwrite it
    # with the currently booted host's metadata during rollback/republication.
    if not (directory / "nix/var/nix/db/db.sqlite").exists():
        seed_metadata(Path("/nix/var/nix/db/db.sqlite"), directory / "seed-metadata.sqlite")


def publication_registration(system):
    """Freeze the target closure and existing host additions into one view.

    References of writable objects may live in the current immutable base.
    Include those objects now, while the current store is available, rather
    than making a future boot discover and mount historical host views.
    A failed/racing read aborts publication before exposing a boot record.
    """
    from ..host_state import reference_closure
    # An addition published by a previous update now lives in the immutable
    # lower view, so enumerating the upper alone loses it on the next update.
    # Retain the current store's live roots (including user profiles) regardless
    # of their physical layer. --print-live only queries; it collects nothing.
    # Query before dumping registration so a concurrently registered live root
    # cannot be omitted from the registration snapshot used below.
    live = set(command("nix-store", "--store", overlay_uri(),
                       "--gc", "--print-live").splitlines())
    records = builder.registration_records(
        command("nix-store", "--store", overlay_uri(), "--dump-db"))
    upper = Path(HOST) / "active/upper"
    saved = {"/nix/store/" + path.name for path in upper.iterdir()
             if "/nix/store/" + path.name in records}
    paths = reference_closure(records, saved | live | {system})
    return "".join(records[path] for path in sorted(paths))


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
            from ..host_artifacts import forget
            forget(root, "/boot", system)
            return {"system": system, "retired": True}
        record = validate_boot(root, system, allow_retired=True)
        core.atomic_json(path, dict(record, retired=True))
        from ..host_artifacts import forget
        forget(root, "/boot", system)
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
    parser.add_argument("action", choices=("validate", "publish", "select", "seal-boot", "retire", "status", "recover",
                                           "checkpoint-machine", "bind-machine", "install-boot"))
    parser.add_argument("system", nargs="?")
    args = parser.parse_args()
    if args.action in ("publish", "select", "retire", "recover", "bind-machine", "install-boot") and not args.system:
        parser.error("system is required")
    if args.action == "install-boot":
        result = {"installed": args.system}
        subprocess.run(["unshare", "--mount", "--propagation", "private", sys.executable,
                        str(Path(__file__).resolve().parent.parent / "host_install.py"), args.system], check=True)
    elif args.action in ("checkpoint-machine", "bind-machine"):
        from .. import host_machine
        with admission_lock(POOL):
            if args.action == "bind-machine":
                record = validate_boot(POOL, args.system)
            else:
                record = core.read_json(Path(POOL) / "host/running.json")
                selection = Path(host_machine.MACHINE) / "selection.json"
                if selection.exists():
                    selected = core.read_json(selection)
                    manifest = core.read_json(Path(selected["release"]) / "release.json")
                    if manifest["host"] != record["system"]:
                        # Another release was applied but not yet booted. Its
                        # binding is already recorded; keep the running host's.
                        print(json.dumps({"checkpoint": "unchanged", "system": record["system"]}))
                        return
            host_machine.save(POOL, HOST, host_machine.MACHINE, record)
        result = {"bound": record["system"]}
    elif args.action == "validate":
        if not args.system:
            parser.error("system is required")
        with admission_lock(POOL):
            result = validate_boot(POOL, args.system)
    elif args.action == "publish":
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
                  "upper_allocated_bytes": allocated(Path(HOST) / "active/upper"),
                  "generation_state_allocated_bytes": allocated(Path(HOST) / "generations"),
                  "recovery_allocated_bytes": allocated("/var/lib/airlock/recovery-store")}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
