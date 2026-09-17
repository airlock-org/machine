import io
import json
import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_SOURCE = ROOT / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"
sys.path.insert(0, str(ENGINE_SOURCE))

from airlock_qubes_storage._engine import (
    artifacts,
    core,
    generations,
    maintenance,
)  # noqa: E402


@pytest.fixture
def artifact_input(tmp_path, monkeypatch):
    physical = tmp_path / "nix/store"
    physical.mkdir(parents=True)
    common = "/nix/store/" + "a" * 32 + "-common"
    system = "/nix/store/" + "b" * 32 + "-system"
    for logical, content in ((common, "common-content"), (system, "system-content")):
        source = physical / Path(logical).name
        source.mkdir()
        (source / "payload").write_text(content)
    closure = tmp_path / "closure"
    closure.mkdir()
    (closure / "store-paths").write_text(common + "\n" + system + "\n")
    path_info = {
        common: {"references": [], "narHash": "sha256-common", "narSize": 1},
        system: {
            "references": [common],
            "narHash": "sha256-system",
            "narSize": 2,
        },
    }

    def fake_view(
        root,
        domain,
        generation,
        paths,
        closure_id,
        view_backend,
        txid,
        target_bytes,
        trusted_paths=frozenset(),
    ):
        canonical = Path(root) / "views/by-closure" / closure_id
        canonical.mkdir(parents=True, exist_ok=True)
        image = canonical / "store.erofs"
        image.write_bytes(b"test-view")
        blob_map = canonical / "store.blob-map.json"
        core.atomic_json(blob_map, {"segments": [], "extents": []})
        object_refs = canonical / "object-refs"
        object_refs.write_text("".join(path + "\n" for path in paths))
        generation_dir = (
            Path(root) / "domains" / domain / "generations" / str(generation)
        )
        result = {}
        for name, source in (
            ("store.erofs", image),
            ("store.blob-map.json", blob_map),
            ("object-refs", object_refs),
        ):
            target = generation_dir / name
            target.symlink_to(os.path.relpath(source, generation_dir))
            result[name] = str(target)
        return {
            "view_backend": view_backend,
            "filesystem": "erofs",
            "view_dir": str(canonical),
            "image": result["store.erofs"],
            "canonical_image": str(image),
            "metadata_image": result["store.erofs"],
            "canonical_metadata_image": str(image),
            "blob_map": result["store.blob-map.json"],
            "canonical_blob_map": str(blob_map),
            "object_refs": result["object-refs"],
            "canonical_object_refs": str(object_refs),
            "blob_volume": "store-data",
            "blob_logical_size": 0,
            "object_data_bytes": 0,
            "object_extent_count": 0,
            "segment_count": 0,
            "segments": [],
            "image_contains_materialized_store_copy": False,
            "image_created": True,
        }

    monkeypatch.setattr(artifacts, "materialize_view", fake_view)
    output = io.BytesIO()
    artifacts.write_artifact(
        output,
        domain="usr-home",
        system=system,
        closure_source=closure,
        root_source="/nix/store/" + "c" * 32 + "-root/root.img",
        kernel_name="test-kernel",
        source_revision="1" * 40,
        lock_hash="2" * 64,
        physical_store_root=physical,
        path_info=path_info,
    )
    return {
        "bytes": output.getvalue(),
        "pool": tmp_path / "pool",
        "common": common,
        "system": system,
        "closure": str(closure),
    }


def rewrite_artifact(data, transform):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as source:
        members = []
        for member in source:
            payload = source.extractfile(member).read()
            members.append((member.name, payload))
    members = transform(members)
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as target:
        for name, payload in members:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o400
            member.mtime = 1
            target.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def receive(case, data=None):
    return artifacts.receive_artifact(
        io.BytesIO(case["bytes"] if data is None else data),
        pool_root=case["pool"],
        expected_domain="usr-home",
        approved_by="sys-dev",
    )


def test_transferred_artifact_is_inactive_retained_and_reused(artifact_input):
    first = receive(artifact_input)
    assert first["activated"] is False
    assert first["retained"] is True
    assert first["current_generation"] is None
    assert first["objects_admitted"] == 2
    second = receive(artifact_input)
    assert second["objects_admitted"] == 0
    assert second["objects_reused"] == 2
    assert len(list((artifact_input["pool"] / "objects/metadata").glob("*.json"))) == 2
    manifest = core.read_json(second["manifest"])
    assert manifest["approval"] == {
        "approved_by": "sys-dev",
        "source": artifact_input["closure"],
        "transport": "qrexec",
    }
    match = generations.find_generation(
        SimpleNamespace(
            pool_root=artifact_input["pool"],
            domain="usr-home",
            system=artifact_input["system"],
            closure_source=artifact_input["closure"],
            root_source="/nix/store/" + "c" * 32 + "-root/root.img",
            kernel_name="test-kernel",
            approved_by="sys-dev",
            view_backend="erofs-objectmap",
        )
    )
    assert match["status"] == "found"
    assert match["generation"] == second["generation"]

    gc_result = maintenance.gc(
        SimpleNamespace(pool_root=artifact_input["pool"], keep_generations=0)
    )
    assert len(gc_result["retained_generations"]) == 2
    assert not gc_result["removed_object_metadata"]


@pytest.mark.parametrize(
    "damage", ["payload", "missing", "unsafe", "incomplete", "truncated"]
)
def test_transferred_artifact_rejects_invalid_input(artifact_input, damage):
    if damage == "payload":
        data = artifact_input["bytes"].replace(b"common-content", b"broken-content", 1)
    elif damage == "missing":
        data = rewrite_artifact(
            artifact_input["bytes"],
            lambda members: [
                item for item in members if not item[0].endswith("-common.tar")
            ],
        )
    elif damage == "unsafe":
        data = rewrite_artifact(
            artifact_input["bytes"],
            lambda members: [("../escape", b"bad")] + members,
        )
    elif damage == "incomplete":
        missing = "/nix/store/" + "d" * 32 + "-missing"

        def add_missing_reference(members):
            result = []
            for name, payload in members:
                if name == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["objects"][0]["references"] = [missing]
                    payload = (json.dumps(manifest) + "\n").encode()
                result.append((name, payload))
            return result

        data = rewrite_artifact(artifact_input["bytes"], add_missing_reference)
    else:
        data = artifact_input["bytes"][: len(artifact_input["bytes"]) // 2]
    with pytest.raises(core.EngineError):
        receive(artifact_input, data)
    assert not list((artifact_input["pool"] / "domains").glob("*/*/*/manifest.json"))
    assert not list((artifact_input["pool"] / "tmp").iterdir())


def test_transferred_artifact_target_is_checked(artifact_input):
    with pytest.raises(core.EngineError, match="target mismatch"):
        artifacts.receive_artifact(
            io.BytesIO(artifact_input["bytes"]),
            pool_root=artifact_input["pool"],
            expected_domain="sys-gui",
            approved_by="sys-dev",
        )
    assert not list((artifact_input["pool"] / "tmp").iterdir())


def test_interrupted_admission_is_invisible_and_retryable(artifact_input, monkeypatch):
    original = artifacts.maybe_failpoint
    failed = False

    def fail_once(name):
        nonlocal failed
        if name == "artifact-after-object-import" and not failed:
            failed = True
            raise core.EngineError("simulated interruption")
        return original(name)

    monkeypatch.setattr(artifacts, "maybe_failpoint", fail_once)
    with pytest.raises(core.EngineError, match="simulated interruption"):
        receive(artifact_input)
    assert not list((artifact_input["pool"] / "domains").glob("*/*/*/manifest.json"))
    assert not list((artifact_input["pool"] / "tmp").iterdir())
    result = receive(artifact_input)
    assert result["status"] == "passed"
    assert result["objects_reused"] == 1
    assert result["objects_admitted"] == 1
