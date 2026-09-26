"""Offline assembly of dom0's store, before initrd prepares the normal root."""
import argparse
import os
from pathlib import Path
import shutil

from ._engine import builder, core, host, state, views
from ._engine.pool import admission_lock
from ._engine.prepared import relocate
from . import host_state


def boot_system(cmdline):
    values = [word.removeprefix("init=") for word in cmdline.split() if word.startswith("init=")]
    if len(values) != 1 or not values[0].endswith("/init"):
        core.fail("exactly one NixOS init path is required")
    system = values[0].removesuffix("/init")
    # Specialisation boot entries can name a symlink inside the base toplevel.
    # Seeding publishes an alias bound to the actual target; no filesystem
    # resolution through the as-yet-unmounted store is necessary.
    parts = system.split("/specialisation/")
    core.store_entry(parts[0])
    if len(parts) > 2 or (len(parts) == 2 and (not parts[1] or "/" in parts[1] or parts[1] in (".", ".."))):
        core.fail("invalid specialisation init path")
    return system


def assemble(sysroot, cmdline):
    sysroot = Path(sysroot)
    root = sysroot / host.POOL.lstrip("/")
    storage = sysroot / host.HOST.lstrip("/")
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    def physical(path):
        return sysroot / str(path).lstrip("/")
    system = boot_system(cmdline)
    with admission_lock(root):
        if "/specialisation/" in system:
            aliases = core.read_json(root / "host/aliases.json")
            if system not in aliases:
                core.fail("boot specialisation has no published pool binding")
            system = aliases[system]
        selected = host.validate_boot(root, system, logical_root=host.POOL)
        # Compute the lower objects needed by actual saved upper payloads.
        # Cached lower metadata alone does not keep obsolete host generations.
        previous_path = storage / "base.json"
        previous = core.read_json(previous_path) if previous_path.exists() else {"systems": []}
        old_registration = host_state.dump_registration(sysroot)
        old_records = builder.registration_records(old_registration)
        upper = storage / "upper"
        upper.mkdir(exist_ok=True)
        saved = {"/nix/store/" + path.name for path in upper.iterdir()
                 if "/nix/store/" + path.name in old_records}
        records = [selected]
        for path in sorted((root / "host/boots").glob("*.json")):
            record = core.read_json(path)
            if not record.get("retired"):
                records.append(host.validate_boot(root, record["system"], logical_root=host.POOL))
        records = list({r["generation"]: r for r in records}.values())
        registrations = [physical(record["registration"]).read_text() for record in records]
        available = set().union(*(builder.registration_paths(text) for text in registrations))
        # Published copies will be removed below. Only the remaining upper
        # objects and their reference closure require persistent lower pins.
        upper_needed = host_state.reference_closure(old_records, saved - available)
        missing = upper_needed - saved - available
        for item in previous["systems"]:
            if not missing:
                break
            old = host.validate_boot(root, item, logical_root=host.POOL, allow_retired=True)
            text = physical(old["registration"]).read_text()
            paths = builder.registration_paths(text)
            if paths & missing:
                records.append(old)
                registrations.append(text)
                available.update(paths)
                missing -= paths
        if missing:
            core.fail("saved host upper has no retained lower for its references")
        records = list({r["generation"]: r for r in records}.values())
        systems = [record["system"] for record in records]
        registration = builder.merge_registration(*registrations)
        registered_paths = builder.registration_paths(registration)
        upper_registration = "".join(old_records[path] for path in sorted(upper_needed))
        new_registration = builder.merge_registration(registration, upper_registration) if upper_registration else registration
        mounts = []
        try:
            bases = []
            for record in records:
                generation = record["generation"]
                target = storage / "bases" / str(generation)
                target.mkdir(parents=True, exist_ok=True)
                if os.path.ismount(target):
                    core.fail("host base already mounted")
                blob_map = core.read_json(physical(record["blob_map"]))
                mapped = relocate(blob_map, host.POOL, str(root))
                name = f"airlock-host-{generation}"
                device = views.create_dm_view_for_blob_map(root, name, mapped)["device"]
                host.command("mount", "-t", "erofs", "-o", f"ro,loop,device={device}",
                             str(physical(record["metadata_image"])), str(target))
                mounts.append(target)
                bases.append(target)
            lower = storage / "lower"
            lower_store = lower / "nix/store"
            lower_store.mkdir(parents=True, exist_ok=True)
            # Register metadata before the read-only lower mount is visible.
            # The database is reconstructed each boot from checked registrations.
            shutil.rmtree(lower / "nix/var/nix/db", ignore_errors=True)
            host.command("nix-store", "--store", f"local?root={lower}", "--load-db", input=registration)
            if len(bases) == 1:
                host.command("mount", "--bind", str(bases[0]), str(lower_store))
                host.command("mount", "-o", "remount,bind,ro", str(lower_store))
            else:
                host.command("mount", "-t", "overlay", "overlay", "-o",
                             "ro,index=off,metacopy=off,redirect_dir=nofollow,lowerdir=" + ":".join(map(str, bases)),
                             str(lower_store))
            mounts.append(lower_store)
            db = lower / "nix/var/nix/db"
            host.command("mount", "--bind", str(db), str(db))
            mounts.append(db)
            host.command("mount", "-o", "remount,bind,ro", str(db))
            upper, work = storage / "upper", storage / "work"
            upper.mkdir(exist_ok=True)
            work.mkdir(exist_ok=True)
            # Remove identical published copies only with every overlay stopped.
            # Each rename is safe to repeat after power loss: the durable lower
            # and its registration already provide the very same store object.
            trash = storage / "published-copies"
            trash.mkdir(exist_ok=True)
            for path in upper.iterdir():
                logical = "/nix/store/" + path.name
                if logical not in registered_paths:
                    continue
                metadata = core.read_json(root / "objects/metadata" / (path.name + ".json"))
                if core.tree_hash(path) != metadata["tree_hash"]:
                    core.fail(f"upper differs from published immutable object: {logical}")
                destination = trash / path.name
                if destination.exists() or destination.is_symlink():
                    core.remove_path(destination)
                path.rename(destination)
                core.fsync_dir(upper)
            # Persist the new base before removing duplicates. Its old physical
            # references remain pinned even if this boot fails later.
            for record in records:
                blob_map = core.read_json(physical(record["blob_map"]))
                refs = views.blob_map_physical_references(blob_map, "host-upper")
                core.atomic_json(state.running_ref_path(root, "dom0", record["generation"], "host-upper"), {
                    "schema": core.SCHEMA_RUNNING_REF, "status": "running", "domain": "dom0",
                    "generation": record["generation"], "manifest": record["manifest"],
                    "closure_hash": record["closure_hash"], "volume": "host-upper",
                    "physical_references": [ref for group in refs.values() for ref in group],
                })
            host_state.replace_database(sysroot, new_registration)
            core.atomic_json(previous_path, {"schema": "airlock.host-base.v1", "systems": systems})
            shutil.rmtree(trash)
            # Nix's optional optimizer can keep an otherwise unreferenced
            # inode in .links after its last published upper copy is removed.
            links = upper / ".links"
            if links.is_dir() and not links.is_symlink():
                for link in links.iterdir():
                    if link.is_file() and link.lstat().st_nlink == 1:
                        link.unlink()
                core.fsync_dir(links)
            merged = sysroot / "nix/store"
            merged.mkdir(parents=True, exist_ok=True)
            host.command("mount", "-t", "overlay", "overlay", "-o",
                         f"index=off,metacopy=off,redirect_dir=nofollow,lowerdir={lower_store},upperdir={upper},workdir={work}", str(merged))
            mounts.append(merged)
            if not (physical(system) / "init").is_file():
                core.fail("selected system init missing from assembled host view")
            core.atomic_json(root / "host/running.json", selected)
            # Only after registration, mounts and base identity are committed
            # may GC release physical references from the previous lower view.
            keep = {record["generation"] for record in records}
            for pin in (root / "running/dom0").glob("*/host-upper.json"):
                if int(pin.parent.name) not in keep:
                    state.clear_running_ref(root, "dom0", int(pin.parent.name), "host-upper")
            host.collect_retired(root)

        except BaseException:
            for target in reversed(mounts):
                host.command("umount", str(target))
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sysroot", default="/sysroot")
    args = parser.parse_args()
    assemble(args.sysroot, Path("/proc/cmdline").read_text())


if __name__ == "__main__":
    main()
