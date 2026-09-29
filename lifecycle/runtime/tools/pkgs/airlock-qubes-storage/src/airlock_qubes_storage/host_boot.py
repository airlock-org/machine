"""Offline assembly of dom0's store, before initrd prepares the normal root."""
import argparse
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

from ._engine import builder, core, host, state, views
from ._engine.pool import admission_lock
from ._engine.prepared import relocate
from . import host_state


def progress(message):
    print(f"Airlock host store [{time.monotonic():.3f}s]: {message}", flush=True)


def record_mount_failure(sysroot, error):
    """Save evidence before cleanup; diagnostics must not replace the failure."""
    try:
        report = {"error": repr(error), "traceback": traceback.format_exc()}
        if isinstance(error, subprocess.CalledProcessError):
            report.update(command=error.cmd, returncode=error.returncode,
                          stdout=error.stdout, stderr=error.stderr)
        for name in ("/proc/self/mountinfo", "/proc/filesystems", "/proc/cmdline"):
            try:
                report[name] = Path(name).read_text()
            except OSError as diagnostic_error:
                report[name] = repr(diagnostic_error)
        try:
            result = subprocess.run(["dmesg", "--color=never"], capture_output=True,
                                    text=True, errors="replace", timeout=5)
            report["kernel"] = result.stdout[-65536:]
            report["kernel_stderr"] = result.stderr[-4096:]
            report["kernel_returncode"] = result.returncode
        except (OSError, subprocess.TimeoutExpired) as diagnostic_error:
            report["kernel_error"] = repr(diagnostic_error)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        directory = Path(sysroot) / "var/log/airlock-boot"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{boot_id}-host-store-failure.json"
        core.atomic_json(path, report, mode=0o600)
        progress(f"saved mount failure evidence to {path}")
    except Exception as diagnostic_error:
        progress(f"could not save mount failure evidence: {diagnostic_error}")


def mount_overlay_in_root(sysroot):
    """Mount in a chrooted child so saved OverlayFS options survive switch-root.

    Load libc before chroot: the destination store is not mounted yet, so no
    executable or dynamic library can be loaded from it at this point.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    mount = libc.mount
    mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                      ctypes.c_ulong, ctypes.c_char_p]
    mount.restype = ctypes.c_int
    options = ("index=off,metacopy=off,redirect_dir=nofollow,"
               f"lowerdir={host.HOST}/lower/nix/store,"
               f"upperdir={host.HOST}/upper,workdir={host.HOST}/work")
    os.chroot(sysroot)
    os.chdir("/")
    if mount(b"overlay", b"/nix/store", b"overlay", 0, options.encode()) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "/nix/store")


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
    progress(f"starting assembly for {system}")
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
        progress("validated boot record; reading previous Nix database")
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
        progress(f"validated {len(records)} bases and {len(registered_paths)} store paths")
        mounts = []
        try:
            bases = []
            for record in records:
                generation = record["generation"]
                progress(f"mounting EROFS base {generation}")
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
            # Reconstruct metadata from checked registrations each boot.
            shutil.rmtree(lower / "nix/var/nix/db", ignore_errors=True)
            # LocalStore opens .links even with a read-only database. Keep
            # this empty administrative directory outside sealed object views.
            scaffold = storage / "lower-scaffold"
            (scaffold / ".links").mkdir(parents=True, exist_ok=True)
            host.command("mount", "-t", "overlay", "overlay", "-o",
                         "ro,index=off,metacopy=off,redirect_dir=nofollow,lowerdir="
                         + ":".join(map(str, [*bases, scaffold])), str(lower_store))
            mounts.append(lower_store)
            # --load-db needs readable .drv contents, not just their records.
            progress("loading lower Nix database")
            host.command("nix-store", "--store", f"local?root={lower}", "--load-db", input=registration)
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
            # Assemble payloads before staging registration: Nix reads saved
            # derivations while rebuilding the database. No stage-2 process is
            # running yet, and failure cleanup unmounts this view.
            progress("mounting final host overlay")
            merged = sysroot / "nix/store"
            merged.mkdir(parents=True, exist_ok=True)
            host.command(sys.executable, "-m", "airlock_qubes_storage.host_boot",
                         "--mount-overlay", "--sysroot", str(sysroot))
            mounts.append(merged)
            progress("replacing host Nix database")
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
            progress("assembly complete; ready for NixOS closure discovery and activation")

        except BaseException as error:
            record_mount_failure(sysroot, error)
            for target in reversed(mounts):
                try:
                    host.command("umount", str(target))
                except Exception as cleanup_error:
                    progress(f"cleanup failed for {target}: {cleanup_error}")
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sysroot", default="/sysroot")
    parser.add_argument("--mount-overlay", action="store_true")
    args = parser.parse_args()
    if args.mount_overlay:
        mount_overlay_in_root(args.sysroot)
        return
    try:
        assemble(args.sysroot, Path("/proc/cmdline").read_text())
    except BaseException as error:
        progress("FAILED during host store assembly")
        if isinstance(error, subprocess.CalledProcessError):
            print(f"Command: {error.cmd!r}\nstdout: {error.stdout}\nstderr: {error.stderr}", flush=True)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
