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
from . import host_machine, host_state


def progress(message):
    print(f"Airlock host store [{time.monotonic():.3f}s]: {message}", flush=True)


def record_mount_failure(sysroot, error):
    """Save evidence before cleanup; diagnostics must not replace the failure."""
    try:
        report = {"error": repr(error), "traceback": traceback.format_exc()}
        if isinstance(error, OSError):
            report.update(errno=error.errno, filename=error.filename)
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


def mount_lower_view(base, scaffold, target):
    """Expose the exact syscall errno, which mount(8)'s exit 32 loses."""
    libc = ctypes.CDLL(None, use_errno=True)
    mount = libc.mount
    mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                      ctypes.c_ulong, ctypes.c_char_p]
    mount.restype = ctypes.c_int
    options = ("index=off,metacopy=off,redirect_dir=nofollow,"
               f"lowerdir={base}:{scaffold}")
    if mount(b"overlay", os.fsencode(target), b"overlay", 1, options.encode()) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{os.strerror(error)}; options={options}", str(target))


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
               f"upperdir={host.HOST}/active/upper,workdir={host.HOST}/active/work")
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
        # The boot entry names one immutable view. Never enumerate other
        # boot records here: an unrelated missing/corrupt view is not a boot
        # dependency, even when retained for rollback.
        generation = selected["generation"]
        if type(generation) is not int or generation < 1:
            core.fail("invalid host generation")
        generation_root = storage / "generations" / str(generation)
        generation_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        upper = generation_root / "upper"
        work = generation_root / "work"
        upper.mkdir(exist_ok=True)
        work.mkdir(exist_ok=True)
        registration = physical(selected["registration"]).read_text()
        registered_paths = builder.registration_paths(registration)
        # Writable state belongs to this generation, too. In particular, do
        # not open the database left by whichever generation ran most recently.
        host_state.recover_database(generation_root)
        # First boot has no previous database. Keep validation strict when a
        # database does exist, without parsing an absent one as a closure.
        old_records = (builder.registration_records(host_state.dump_registration(generation_root))
                       if (generation_root / "nix/var/nix/db/db.sqlite").exists() else {})
        saved = {"/nix/store/" + path.name for path in upper.iterdir()
                 if "/nix/store/" + path.name in old_records}
        upper_needed = host_state.reference_closure(old_records, saved)
        if upper_needed - saved - registered_paths:
            core.fail("host generation upper has references outside its fixed view")
        upper_registration = "".join(old_records[path] for path in sorted(upper_needed))
        new_registration = (builder.merge_registration(registration, upper_registration)
                            if upper_needed else registration)
        progress(f"validated fixed base {generation} and {len(registered_paths)} store paths")
        mounts = []
        try:
            target = storage / "bases" / str(generation)
            target.mkdir(parents=True, exist_ok=True)
            if os.path.ismount(target):
                core.fail("host base already mounted")
            blob_map = core.read_json(physical(selected["blob_map"]))
            mapped = relocate(blob_map, host.POOL, str(root))
            device = views.create_dm_view_for_blob_map(root, f"airlock-host-{generation}", mapped)["device"]
            progress(f"mounting EROFS base {generation}")
            host.command("mount", "-t", "erofs", "-o", f"ro,loop,device={device}",
                         str(physical(selected["metadata_image"])), str(target))
            mounts.append(target)
            lower = storage / "lower"
            lower_store = lower / "nix/store"
            lower_store.mkdir(parents=True, exist_ok=True)
            # Reconstruct metadata from checked registrations each boot.
            shutil.rmtree(lower / "nix/var/nix/db", ignore_errors=True)
            # LocalStore opens .links even with a read-only database. Keep
            # this empty administrative directory outside sealed object views.
            scaffold = storage / "lower-scaffold"
            (scaffold / ".links").mkdir(parents=True, exist_ok=True)
            progress("mounting fixed lower view")
            mount_lower_view(target, scaffold, lower_store)
            mounts.append(lower_store)
            # --load-db needs readable .drv contents, not just their records.
            progress("loading lower Nix database")
            host.command("nix-store", "--store", f"local?root={lower}", "--load-db", input=registration)
            db = lower / "nix/var/nix/db"
            host.command("mount", "--bind", str(db), str(db))
            mounts.append(db)
            host.command("mount", "-o", "remount,bind,ro", str(db))
            # One bind mount keeps upper/work on the same mount, as required
            # by OverlayFS, and gives the Nix daemon a stable store URI.
            active = storage / "active"
            active.mkdir(exist_ok=True)
            host.command("mount", "--bind", str(generation_root), str(active))
            mounts.append(active)
            # Remove identical published copies only with every overlay stopped.
            # Each rename is safe to repeat after power loss: the durable lower
            # and its registration already provide the very same store object.
            trash = generation_root / "published-copies"
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
            # Pin just this active view. Retained generations have independent
            # retention pins; they are never mounted as a side effect of boot.
            refs = views.blob_map_physical_references(blob_map, "host-upper")
            core.atomic_json(state.running_ref_path(root, "dom0", generation, "host-upper"), {
                "schema": core.SCHEMA_RUNNING_REF, "status": "running", "domain": "dom0",
                "generation": generation, "manifest": selected["manifest"],
                "closure_hash": selected["closure_hash"], "volume": "host-upper",
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
            own_database = generation_root / "nix/var/nix/db/db.sqlite"
            metadata_source = own_database if own_database.exists() else generation_root / "seed-metadata.sqlite"
            host_state.replace_database(sysroot, new_registration, state_root=generation_root,
                                        metadata_source=metadata_source)
            database = sysroot / "nix/var/nix/db"
            database.mkdir(parents=True, exist_ok=True)
            host.command("mount", "--bind", str(generation_root / "nix/var/nix/db"), str(database))
            mounts.append(database)
            core.atomic_json(storage / "base.json", {
                "schema": "airlock.host-base.v2", "systems": [selected["system"]],
                "generation": generation, "closure_hash": selected["closure_hash"],
            })
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
            progress("restoring the booted generation's machine selection")
            host_machine.restore(sysroot, selected)
            core.atomic_json(root / "host/running.json", selected)
            # Only after registration, mounts and base identity are committed
            # may GC release physical references from the previous lower view.
            for pin in (root / "running/dom0").glob("*/host-upper.json"):
                if int(pin.parent.name) != generation:
                    state.clear_running_ref(root, "dom0", int(pin.parent.name), "host-upper")
            # Retirement/GC belongs to explicit maintenance, never early boot.
            progress("assembly complete; ready for NixOS closure discovery and activation")

        except BaseException as error:
            record_mount_failure(sysroot, error)
            error._airlock_mount_report_attempted = True
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
        if not getattr(error, "_airlock_mount_report_attempted", False):
            record_mount_failure(args.sysroot, error)
        if isinstance(error, subprocess.CalledProcessError):
            print(f"Command: {error.cmd!r}\nstdout: {error.stdout}\nstderr: {error.stderr}", flush=True)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
