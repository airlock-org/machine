"""Mount a builder role's pinned pool base plus its private Nix upper."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
from urllib.parse import quote


def checked(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def is_mountpoint(path, mountinfo=Path("/proc/self/mountinfo")):
    """Recognize same-filesystem bind mounts, including the read-only DB bind."""
    target = os.path.abspath(path)
    for line in mountinfo.read_text().splitlines():
        mount_path = re.sub(
            r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]
        )
        if mount_path == target:
            return True
    return False


def read_identity(metadata, domain="sys-agent"):
    identity = json.loads((metadata / "identity.json").read_text())
    registration = (metadata / "registration").read_bytes()
    if (
        identity.get("schema") != f"airlock.{domain}-builder.v1"
        or identity.get("domain") != f"{domain}-builder"
        or hashlib.sha256(registration).hexdigest()
        != identity.get("registration_sha256")
    ):
        raise RuntimeError("invalid pinned builder metadata")
    if "flake_inputs_sha256" in identity:
        flake_inputs = (metadata / "flake-inputs.json").read_bytes()
        if hashlib.sha256(flake_inputs).hexdigest() != identity["flake_inputs_sha256"]:
            raise RuntimeError("invalid pinned builder flake inputs")
    return identity, registration


def record_base(state, identity):
    """Accept only a dom0-authorized additive transition before mounting upper."""
    target = state / "base.json"
    if target.exists():
        previous = json.loads(target.read_text())
        if previous == identity:
            return
        binding = {key: previous.get(key) for key in ("generation", "closure_hash", "registration_sha256")}
        if any(value is None for value in binding.values()) or binding not in identity.get("predecessors", []):
            raise RuntimeError(
                "builder base changed; preserve the old base and upper, do not rebase automatically"
            )
    else:
        # First initialization is safe only with no existing overlay state.
        for name in ("upper", "merged", "work"):
            directory = state / name
            if directory.exists() and any(directory.iterdir()):
                raise RuntimeError("builder state exists without its base identity")
    temporary = state / ".base.json.tmp"
    with temporary.open("w") as stream:
        json.dump(identity, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    fd = os.open(state, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_foreign_base(state, identity):
    """Keep a cloned builder's former upper separate from the new role's base."""
    target = state / "base.json"
    if not target.exists():
        return
    previous = json.loads(target.read_text())
    domain = previous.get("domain")
    if domain == identity["domain"]:
        return
    if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9_-]+-builder", domain):
        raise RuntimeError("invalid inherited builder domain")
    archive = state.with_name(state.name + "-" + domain)
    if archive.exists():
        raise RuntimeError("inherited builder archive already exists; preserve both layers")
    state.rename(archive)
    state.mkdir(mode=0o755)
    fd = os.open(state.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def overlay_uri(runtime, state):
    lower = f"local?root={runtime}/lower&read-only=true"
    return (
        f"local-overlay://?root={state}/merged&lower-store="
        f"{quote(lower, safe='')}&upper-layer={state}/upper"
    )


def start(args):
    state, runtime = Path(args.state), Path(args.runtime)
    user = pwd.getpwnam(args.user)
    state.mkdir(mode=0o755, parents=True, exist_ok=True)
    runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != 0 or state.stat().st_mode & 0o022:
        raise RuntimeError("builder state must be a root-owned directory")
    if (runtime / "ready.json").exists():
        raise RuntimeError("builder store is already mounted")
    mounts = []

    def mount(target, *options):
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or is_mountpoint(target):
            raise RuntimeError(f"unsafe or already mounted builder path: {target}")
        checked("mount", *options, str(target))
        mounts.append(str(target))

    try:
        pool = runtime / "pool"
        mount(
            pool,
            "-t",
            "erofs",
            "-o",
            f"ro,nodev,nosuid,device={args.blob_device}",
            args.store_device,
        )
        identity, registration = read_identity(
            pool / ".airlock-builder", getattr(args, "domain", "sys-agent")
        )
        archive_foreign_base(state, identity)
        record_base(state, identity)
        lower = runtime / "lower"
        # Nix reads .drv contents during --load-db. Expose the sealed objects
        # first, with the empty .links directory required by LocalStore outside
        # the immutable pool, just as in dom0's lower store.
        scaffold = runtime / "lower-scaffold"
        (scaffold / ".links").mkdir(parents=True, exist_ok=True)
        mount(
            lower / "nix/store",
            "-t", "overlay", "overlay", "-o",
            f"ro,nodev,nosuid,index=off,metacopy=off,redirect_dir=nofollow,lowerdir={pool}:{scaffold}",
        )
        # Register metadata only; package bytes remain in the shared pool.
        # The lower database is rebuilt from its approved index each boot.
        checked(
            args.nix_store,
            "--store",
            str(lower),
            # No builds run here. Avoid LocalStore trying to chown the sealed
            # mount to nixbld while creating this metadata-only database.
            "--option", "build-users-group", "",
            "--load-db",
            input=registration.decode(),
        )
        db = lower / "nix/var/nix/db"
        checked("mount", "--bind", str(db), str(db))
        mounts.append(str(db))
        checked("mount", "-o", "remount,bind,ro", str(db))
        for name in ("merged", "merged/nix", "merged/nix/store", "upper", "work"):
            path = state / name
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise RuntimeError(f"symlink in builder state: {path}")
            os.chown(path, user.pw_uid, user.pw_gid)
        mount(
            state / "merged/nix/store",
            "-t",
            "overlay",
            "overlay",
            "-o",
            f"index=off,metacopy=off,redirect_dir=nofollow,lowerdir={lower}/nix/store,upperdir={state}/upper,workdir={state}/work",
        )
        # Consumers use this root-owned record; never fall back to an empty
        # private store if metadata or mounts failed.
        ready = {
            "identity": identity,
            "uri": overlay_uri(runtime, state),
            "mounts": mounts,
        }
        temporary = runtime / ".ready.json.tmp"
        temporary.write_text(json.dumps(ready) + "\n")
        os.replace(temporary, runtime / "ready.json")
    except BaseException as error:
        for path in reversed(mounts):
            try:
                checked("umount", path)
            except Exception as cleanup_error:
                message = f"Builder cleanup failed for {path}: {cleanup_error}"
                print(message, file=sys.stderr)
                error.add_note(message)
        raise


def stop(args):
    ready = Path(args.runtime) / "ready.json"
    if not ready.exists():
        return
    data = json.loads(ready.read_text())
    for path in reversed(data["mounts"]):
        if is_mountpoint(path):
            checked("umount", path)
    ready.unlink()
    # Keep upper, Nix DB, base identity AND dom0's persistent physical pin.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--state", default="/rw/airlock-builder")
    parser.add_argument("--runtime", default="/run/airlock-builder")
    parser.add_argument("--user", default="user")
    parser.add_argument("--domain", choices=("sys-agent", "sys-ctrl"), default="sys-agent")
    parser.add_argument("--store-device", default="/dev/xvdg")
    parser.add_argument("--blob-device", default="/dev/xvdh")
    parser.add_argument("--nix-store", default="nix-store")
    args = parser.parse_args()
    (start if args.command == "start" else stop)(args)


if __name__ == "__main__":
    main()
