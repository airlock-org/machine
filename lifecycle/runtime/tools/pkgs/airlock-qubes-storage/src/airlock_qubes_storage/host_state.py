"""Offline host database transitions using Nix's dump/load registration format."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from ._engine import core, host


def reference_closure(records, roots):
    needed = set()
    pending = list(roots)
    while pending:
        path = pending.pop()
        if path in needed:
            continue
        if path not in records:
            core.fail(f"saved upper references unregistered object: {path}")
        needed.add(path)
        pending.extend(records[path].splitlines()[5:])
    return needed


def recover_database(sysroot):
    state = Path(sysroot) / "nix/var/nix"
    db, previous, pending = state / "db", state / "airlock-db-previous", state / "airlock-db-pending"
    if not db.exists() and previous.exists():
        previous.rename(db)
        core.fsync_dir(state)
    # db always represents a complete old or complete replacement database.
    if db.exists():
        shutil.rmtree(previous, ignore_errors=True)
    shutil.rmtree(pending, ignore_errors=True)


def dump_registration(sysroot):
    recover_database(sysroot)
    db = Path(sysroot) / "nix/var/nix/db/db.sqlite"
    if not db.exists():
        core.fail("host upper registration database is missing")
    return host.command("nix-store", "--store", f"local?root={sysroot}", "--dump-db")


def seed_metadata(source, destination):
    """Snapshot live SQLite metadata, including its WAL, before first boot."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        temporary = Path(directory) / 'db.sqlite'
        old = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        new = sqlite3.connect(temporary)
        try:
            old.backup(new)
        finally:
            new.close()
            old.close()
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.rename(destination)
        core.fsync_dir(destination.parent)


def replace_database(sysroot, registration, *, state_root=None, metadata_source=None):
    """Rebuild offline while preserving ancillary validity and realization data.

    Nix creates its own schema and reference rows. Additional validity fields
    are copied by path, and realization records by column name (not path IDs).
    Refuse an unsupported realization schema rather than losing registrations.
    """
    sysroot = Path(sysroot)
    state = Path(state_root or sysroot) / "nix/var/nix"
    state.mkdir(parents=True, exist_ok=True)
    staging = state / "airlock-db-pending"
    staging.mkdir()
    # Nix parses registered .drv files to rebuild DerivationOutputs. An empty
    # staging store cannot load a database containing saved build recipes.
    # The caller has assembled the future store before host services start.
    store = staging / "nix/store"
    store.mkdir(parents=True)
    host.command("mount", "--bind", str(sysroot / "nix/store"), str(store))
    try:
        host.command("mount", "-o", "remount,bind,ro", str(store))
        host.command("nix-store", "--store", f"local?root={staging}", "--load-db", input=registration)
    finally:
        host.command("umount", str(store))
    old_db = state / "db"
    new_db = staging / "nix/var/nix/db"
    # Preserve trust metadata omitted by --dump-db. Reading the stopped old
    # database includes any committed WAL from an interrupted prior boot.
    metadata_source = Path(metadata_source) if metadata_source else old_db / "db.sqlite"
    if metadata_source.exists():
        with sqlite3.connect(metadata_source.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(new_db / "db.sqlite") as target:
            columns = {row[1] for row in source.execute("PRAGMA table_info(ValidPaths)")}
            target_columns = {row[1] for row in target.execute("PRAGMA table_info(ValidPaths)")}
            extras = [name for name in ("registrationTime", "ultimate", "sigs", "ca") if name in columns & target_columns]
            if extras:
                query = "SELECT path," + ",".join(extras) + " FROM ValidPaths"
                update = "UPDATE ValidPaths SET " + ",".join(name + "=?" for name in extras) + " WHERE path=?"
                for row in source.execute(query):
                    target.execute(update, (*row[1:], row[0]))
            # CA realizations have their own identifiers. Keep only realizations
            # whose output paths survive and preserve dependency relationships.
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            old_paths = dict(source.execute("SELECT id,path FROM ValidPaths"))
            new_paths = dict(target.execute("SELECT path,id FROM ValidPaths"))
            if "DerivationOutputs" in tables:
                for drv, name, path in source.execute("SELECT drv,id,path FROM DerivationOutputs"):
                    if old_paths.get(drv) in new_paths:
                        target.execute("INSERT OR REPLACE INTO DerivationOutputs (drv,id,path) VALUES (?,?,?)",
                                       (new_paths[old_paths[drv]], name, path))
            if "Realisations" in tables:
                real_columns = [r[1] for r in source.execute("PRAGMA table_info(Realisations)")]
                if "outputPath" not in real_columns:
                    core.fail("unsupported Nix realization schema during offline transition")
                retained_ids = set()
                for row in source.execute("SELECT * FROM Realisations"):
                    data = dict(zip(real_columns, row))
                    path = old_paths.get(data["outputPath"])
                    if path not in new_paths:
                        continue
                    data["outputPath"] = new_paths[path]
                    target.execute("INSERT OR REPLACE INTO Realisations (" + ",".join(real_columns) + ") VALUES (" + ",".join("?" for _ in real_columns) + ")", tuple(data[c] for c in real_columns))
                    retained_ids.add(data["id"])
                if "RealisationRefs" in tables:
                    for referrer, reference in source.execute("SELECT referrer,realisationReference FROM RealisationRefs"):
                        if referrer in retained_ids and reference in retained_ids:
                            target.execute("INSERT OR IGNORE INTO RealisationRefs (referrer,realisationReference) VALUES (?,?)", (referrer, reference))
            target.commit()
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.close()
        target.close()
    for file in new_db.iterdir():
        if file.is_file():
            with file.open("rb") as stream:
                os.fsync(stream.fileno())
    core.fsync_dir(new_db)
    previous = state / "airlock-db-previous"
    core.maybe_failpoint("host-before-database-replace")
    if old_db.exists():
        old_db.rename(previous)
        core.fsync_dir(state)
    new_db.rename(old_db)
    core.fsync_dir(state)
    # A failure between renames restores previous on the next early boot.
    core.maybe_failpoint("host-after-database-replace")
    if previous.exists():
        shutil.rmtree(previous)
    shutil.rmtree(staging)
