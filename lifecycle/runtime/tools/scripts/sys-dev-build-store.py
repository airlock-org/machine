"""Mount sys-dev's pinned pool base plus its private persistent Nix upper."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
from urllib.parse import quote


def checked(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def read_identity(metadata):
    identity = json.loads((metadata / "identity.json").read_text())
    registration = (metadata / "registration").read_bytes()
    if (
        identity.get("schema") != "airlock.sys-dev-builder.v1"
        or identity.get("domain") != "sys-dev-builder"
        or hashlib.sha256(registration).hexdigest()
        != identity.get("registration_sha256")
    ):
        raise RuntimeError("invalid pinned builder metadata")
    return identity, registration


def record_base(state, identity):
    """Never silently rebase a persistent upper; fail closed on identity change."""
    target = state / "base.json"
    if target.exists():
        if json.loads(target.read_text()) != identity:
            raise RuntimeError(
                "builder base changed; preserve the old base and upper, do not rebase automatically"
            )
        return
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
        if target.is_symlink() or os.path.ismount(target):
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
        identity, registration = read_identity(pool / ".airlock-builder")
        record_base(state, identity)
        lower = runtime / "lower"
        # --load-db registers metadata, never imports package payloads. State is
        # rebuilt from the immutable combined index each boot, not trusted from
        # guest-writable Nix state. Initialize before covering the empty store.
        checked(
            args.nix_store,
            "--store",
            str(lower),
            "--load-db",
            input=registration.decode(),
        )
        mount(lower / "nix/store", "--bind", str(pool))
        checked("mount", "-o", "remount,bind,ro", str(lower / "nix/store"))
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
    except BaseException:
        for path in reversed(mounts):
            checked("umount", path)
        raise


def stop(args):
    ready = Path(args.runtime) / "ready.json"
    if not ready.exists():
        return
    data = json.loads(ready.read_text())
    for path in reversed(data["mounts"]):
        if os.path.ismount(path):
            checked("umount", path)
    ready.unlink()
    # Keep upper, Nix DB, base identity AND dom0's persistent physical pin.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--state", default="/rw/airlock-builder")
    parser.add_argument("--runtime", default="/run/airlock-builder")
    parser.add_argument("--user", default="user")
    parser.add_argument("--store-device", default="/dev/xvdg")
    parser.add_argument("--blob-device", default="/dev/xvdh")
    parser.add_argument("--nix-store", default="nix-store")
    args = parser.parse_args()
    (start if args.command == "start" else stop)(args)


if __name__ == "__main__":
    main()
