"""Privileged worker for the closure-store-linux-storage pytest check.

Run only inside a private mount namespace. All backings belong to a disposable
pool; device names include a random domain so concurrent checks cannot collide.
"""

import errno
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid
from concurrent.futures import ThreadPoolExecutor


def check(command):
    return subprocess.check_output(command, text=True).strip()


def interrupted_transfers(work, pool, physical, domain, common):
    from airlock_qubes_storage._engine import artifacts, core, state

    def exchange(kwargs, damage):
        client, server = socket.socketpair()
        client.settimeout(10)
        server.settimeout(10)
        with client, server, client.makefile("rwb", buffering=0) as sender, server.makefile("rwb", buffering=0) as receiver:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(artifacts.receive_incremental, receiver, receiver,
                                          pool_root=pool, expected_domain=domain, approved_by="pytest")

                def negotiate(inventory):
                    artifacts.write_message(sender, inventory)
                    return artifacts.read_message(sender)

                try:
                    payload = io.BytesIO()
                    artifacts.write_artifact(payload, negotiate=negotiate, **kwargs)
                    data = payload.getvalue()
                    if damage == "truncated":
                        data = data[:len(data) // 2]
                    elif damage == "corrupt":
                        assert b"transport-probe" in data
                        data = data.replace(b"transport-probe", b"transport-broke", 1)
                    client.sendall(data)
                finally:
                    client.shutdown(socket.SHUT_WR)
                return pending.result(timeout=10)

    original = state.current_generation(pool, domain)
    results = []
    for index, damage in enumerate(("truncated", "corrupt", "artifact-after-object-import",
                                    "artifact-before-generation-commit")):
        new = "/nix/store/" + "c" * 32 + f"-transport-{index}"
        source = physical / Path(new).name
        source.mkdir()
        (source / "payload").write_bytes(b"transport-probe" * 8192)
        closure = work / f"transport-closure-{index}"
        closure.write_text(common + "\n" + new + "\n")
        kwargs = dict(domain=domain, system=new, closure_source=closure, root_source="",
                      kernel_name="", source_revision="1" * 40, lock_hash="2" * 64,
                      physical_store_root=physical,
                      path_info={p: {"references": [], "narHash": "sha256-synthetic-test",
                                     "narSize": 1} for p in (common, new)})
        manifests = {p: p.read_bytes() for p in (pool / "domains").glob("*/generations/*/manifest.json")}
        if damage.startswith("artifact-"):
            os.environ["AIRLOCK_CLOSURE_STORE_FAILPOINT"] = damage
        try:
            try:
                exchange(kwargs, damage)
            except core.EngineError:
                pass
            else:
                raise AssertionError(f"damaged transfer was accepted: {damage}")
        finally:
            os.environ.pop("AIRLOCK_CLOSURE_STORE_FAILPOINT", None)
        assert {p: p.read_bytes() for p in (pool / "domains").glob("*/generations/*/manifest.json")} == manifests
        assert not list((pool / "tmp").iterdir())
        receipt = exchange(kwargs, None)
        assert receipt["status"] == "passed" and receipt["activated"] is False
        assert state.current_generation(pool, domain) == original
        assert not list((pool / "tmp").iterdir())
        results.append(damage)
    return results


def probe(work):
    from airlock_qubes_storage._engine import generations, views

    work = Path(work)
    pool = work / "pool"
    physical = work / "source"
    physical.mkdir()
    common = "/nix/store/" + "a" * 32 + "-common"
    private = "/nix/store/" + "b" * 32 + "-private"
    for path in (common, private):
        target = physical / Path(path).name
        target.mkdir()
        (target / "payload").write_text(path + "\n")
    domain = "linux-probe-" + uuid.uuid4().hex[:12]
    attached = []
    mounted = []
    manifests = []
    try:
        for index, paths in enumerate(((common,), (common, private))):
            closure = work / f"closure-{index}"
            closure.write_text("\n".join(paths) + "\n")
            args = SimpleNamespace(
                pool_root=str(pool), domain=f"{domain}-{index}",
                system=paths[-1], root_source="", kernel_name="",
                closure_source=str(closure), physical_store_root=str(physical),
                segment_target_bytes=1024 * 1024, verify_mode="tree",
                view_backend="erofs-objectmap", approved_by="pytest",
                approval_source="disposable-linux-storage-check", retain=True,
                activate=True, store_mountpoint="/nix/store",
                store_device="/dev/xvde", store_blob_device="/dev/xvdf",
            )
            receipt = generations.admit_generation(args)
            manifest = json.loads(Path(receipt["manifest"]).read_text())
            manifests.append(manifest)
            attachment = views.attach_blob_view(pool, args.domain, receipt["generation"])
            attached.append(attachment)
            assert check(["blockdev", "--getro", attachment["device"]]) == "1"
            mount = work / f"mount-{index}"
            mount.mkdir()
            check(["mount", "-t", "erofs", "-o",
                   f"ro,loop,device={attachment['device']}",
                   manifest["store"]["metadata_image"], str(mount)])
            mounted.append(mount)
            assert sorted(p.name for p in mount.iterdir()) == sorted(Path(p).name for p in paths)
            for path in paths:
                assert (mount / Path(path).name / "payload").read_text() == path + "\n"
            try:
                (mount / Path(common).name / "payload").write_text("modified")
            except OSError as exc:
                assert exc.errno == errno.EROFS, exc
            else:
                raise AssertionError("shared EROFS payload accepted a write")

        def common_extents(manifest):
            data = json.loads(Path(manifest["store"]["canonical_blob_map"]).read_text())
            return [(e["segment_id"], e["segment_offset"], e["length"])
                    for e in data["extents"] if e.get("object") == Path(common).name]

        extents = common_extents(manifests[0])
        assert extents and extents == common_extents(manifests[1])
        # Detaching one view must leave the other view and shared backings usable.
        check(["umount", str(mounted.pop(0))])
        first = attached.pop(0)
        views.detach_blob_view(pool, first["domain"], first["generation"])
        assert not views.dm_exists(first["dm_name"])
        assert (mounted[0] / Path(common).name / "payload").read_text() == common + "\n"
        result = {"schema": "airlock.closure-store-linux-storage.v1", "status": "passed",
                  "real_loop_dm_erofs": True, "shared_extents": extents,
                  "exact_closure_membership": True, "writes_rejected": True,
                  "peer_survives_detach": True}
        result["interrupted_transfer_cleanup_and_retry"] = interrupted_transfers(
            work, pool, physical, f"{domain}-0", common)
    finally:
        for mount in reversed(mounted):
            check(["umount", str(mount)])
        for attachment in reversed(attached):
            views.detach_blob_view(pool, attachment["domain"], attachment["generation"])
            assert not views.dm_exists(attachment["dm_name"])
        # Only loops whose backing files belong to this disposable pool.
        for blob in sorted(pool.glob("objects/segments/*.blob")):
            loops = check(["losetup", "-j", str(blob), "--output", "NAME", "--noheadings"])
            for loop in loops.splitlines():
                check(["losetup", "-d", loop.strip()])
            # Loop removal can finish after losetup returns (udev still has the
            # device open). Bound the cleanup wait before asserting no leaks.
            deadline = time.monotonic() + 5
            while check(["losetup", "-j", str(blob), "--output", "NAME", "--noheadings"]):
                assert time.monotonic() < deadline, f"loop cleanup did not finish: {blob}"
                time.sleep(0.1)
    result["devices_cleaned_up"] = True
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.path.insert(0, sys.argv[1])
    os.environ["AIRLOCK_CLOSURE_STORE_INDEX_SOURCE"] = "dm"
    with tempfile.TemporaryDirectory(prefix="airlock-linux-storage-") as work:
        print(json.dumps(probe(work), sort_keys=True))
