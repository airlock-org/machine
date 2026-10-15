import io
import json
import os
import sys
import tarfile
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_SOURCE = ROOT / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"
sys.path.insert(0, str(ENGINE_SOURCE))

from airlock_qubes_storage._engine import (  # noqa: E402
    artifacts,
    core,
    generations,
    maintenance,
)


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
            "index_depth": 0,
        }

    monkeypatch.setattr(artifacts, "materialize_view", fake_view)
    root_image = physical / ("c" * 32 + "-root") / "root.img"
    root_image.parent.mkdir()
    with root_image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(1080)
        stream.write(b"\x53\xef")
    output = io.BytesIO()
    kwargs = dict(
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
    artifacts.write_artifact(output, **kwargs)
    return {
        "bytes": output.getvalue(),
        "pool": tmp_path / "pool",
        "common": common,
        "system": system,
        "closure": str(closure),
        "kwargs": kwargs,
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
        approved_by="usr-assistant",
    )


def test_transferred_artifact_is_inactive_retained_and_reused(artifact_input):
    first = receive(artifact_input)
    assert first["activated"] is False
    assert first["retained"] is True
    assert first["current_generation"] is None
    assert first["objects_admitted"] == 2
    second = receive(artifact_input)
    assert second["generation"] == first["generation"]
    assert second["objects_admitted"] == 0
    assert second["objects_reused"] == 2
    assert len(list((artifact_input["pool"] / "objects/metadata").glob("*.json"))) == 2
    manifest = core.read_json(second["manifest"])
    assert manifest["store"]["index_depth"] == 0
    assert manifest["approval"] == {
        "approved_by": "usr-assistant",
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
            approved_by="usr-assistant",
            view_backend="erofs-objectmap",
        )
    )
    assert match["status"] == "found"
    assert match["generation"] == second["generation"]

    gc_result = maintenance.gc(
        SimpleNamespace(pool_root=artifact_input["pool"], keep_generations=0)
    )
    assert len(gc_result["retained_generations"]) == 1
    assert not gc_result["removed_object_metadata"]


def test_system_only_artifact_inherits_committed_boot_identity(artifact_input):
    first = receive(artifact_input)
    generations.switch(
        SimpleNamespace(
            pool_root=artifact_input["pool"],
            domain="usr-home",
            generation=first["generation"],
        )
    )
    kwargs = artifact_input["kwargs"]
    expected_root = kwargs["root_source"]
    expected_kernel = kwargs["kernel_name"]
    kwargs["root_source"] = ""
    kwargs["kernel_name"] = ""
    output = io.BytesIO()
    artifacts.write_artifact(output, **kwargs)
    result = artifacts.receive_artifact(
        io.BytesIO(output.getvalue()),
        pool_root=artifact_input["pool"],
        expected_domain="usr-home",
        approved_by="usr-assistant",
    )
    assert result["root_source"] == expected_root
    assert result["kernel_name"] == expected_kernel
    assert result["current_generation"] == first["generation"]
    assert result["generation"] == first["generation"]
    manifest = core.read_json(result["manifest"])
    assert manifest["root"]["source"] == expected_root
    assert manifest["kernel"]["name"] == expected_kernel


def test_system_only_artifact_requires_existing_boot_identity(artifact_input):
    kwargs = artifact_input["kwargs"]
    kwargs["root_source"] = ""
    kwargs["kernel_name"] = ""
    output = io.BytesIO()
    artifacts.write_artifact(output, **kwargs)
    with pytest.raises(core.EngineError, match="existing generation"):
        artifacts.receive_artifact(
            io.BytesIO(output.getvalue()),
            pool_root=artifact_input["pool"],
            expected_domain="usr-home",
            approved_by="usr-assistant",
        )


def test_describe_generation_exposes_activation_identity(artifact_input):
    result = receive(artifact_input)
    described = generations.describe_generation(
        SimpleNamespace(
            pool_root=artifact_input["pool"],
            domain="usr-home",
            generation=result["generation"],
        )
    )
    assert described["schema"] == "airlock.closure-store.generation-info.v1"
    assert described["selected"] is False
    assert described["system"] == artifact_input["system"]
    assert described["root_source"].endswith("-root/root.img")
    assert described["kernel_name"] == "test-kernel"
    assert described["approval"]["approved_by"] == "usr-assistant"
    assert described["retention"]["retain"] is True


def test_retire_generation_clears_selection_only_without_running_refs(
    artifact_input,
):
    result = receive(artifact_input)
    args = SimpleNamespace(
        pool_root=artifact_input["pool"],
        domain="usr-home",
        generation=result["generation"],
    )
    generations.switch(args)
    running = SimpleNamespace(**vars(args), volume="store")
    generations.mark_running(running)
    with pytest.raises(core.EngineError, match="running references"):
        generations.retire_generation(args)
    generations.clear_running(running)
    retired = generations.retire_generation(args)
    assert retired["status"] == "retired"
    assert generations.describe_generation(args)["selected"] is False
    assert generations.describe_generation(args)["retention"]["retain"] is False


def test_pool_retirement_retry_after_gc_does_not_reuse_generation(artifact_input):
    result = receive(artifact_input)
    args = SimpleNamespace(
        pool_root=artifact_input["pool"],
        domain="usr-home",
        generation=result["generation"],
    )
    generations.switch(args)
    retired = generations.retire_generation(args)
    maintenance.gc(SimpleNamespace(pool_root=args.pool_root, keep_generations=0))
    assert not Path(result["manifest"]).exists()
    assert generations.retire_generation(args) == retired
    new = receive(artifact_input)
    assert new["generation"] > result["generation"]
    generations.switch(
        SimpleNamespace(**{**vars(args), "generation": new["generation"]})
    )
    with pytest.raises(core.EngineError):
        generations.retire_generation(args)


def test_pool_retirement_interruption_preserves_current_until_retry(
    artifact_input, monkeypatch
):
    result = receive(artifact_input)
    args = SimpleNamespace(
        pool_root=artifact_input["pool"],
        domain="usr-home",
        generation=result["generation"],
    )
    generations.switch(args)

    def crash(*_args):
        raise OSError("power loss")

    with monkeypatch.context() as patch:
        patch.setattr(generations, "clear_current", crash)
        with pytest.raises(OSError, match="power loss"):
            generations.retire_generation(args)
    maintenance.gc(SimpleNamespace(pool_root=args.pool_root, keep_generations=0))
    assert Path(result["manifest"]).exists()
    assert generations.retire_generation(args)["status"] == "retired"


@pytest.mark.parametrize("consumer", ["usr-browser", "usr-assistant-builder"])
def test_purged_generation_gc_preserves_shared_consumer_objects(
    artifact_input, consumer, tmp_path
):
    first = receive(artifact_input)
    kwargs = dict(artifact_input["kwargs"])
    other_system = "/nix/store/" + "d" * 32 + "-other-system"
    physical = kwargs["physical_store_root"] / Path(other_system).name
    physical.mkdir()
    (physical / "payload").write_text("other-system-content")
    closure = tmp_path / "other-closure"
    closure.mkdir()
    (closure / "store-paths").write_text(
        artifact_input["common"] + "\n" + other_system + "\n"
    )
    kwargs.update(
        domain=consumer,
        system=other_system,
        closure_source=closure,
        path_info={
            artifact_input["common"]: kwargs["path_info"][artifact_input["common"]],
            other_system: {
                "references": [artifact_input["common"]],
                "narHash": "sha256-other",
                "narSize": 2,
            },
        },
    )
    artifact = io.BytesIO()
    artifacts.write_artifact(artifact, **kwargs)
    second = artifacts.receive_artifact(
        io.BytesIO(artifact.getvalue()),
        pool_root=artifact_input["pool"],
        expected_domain=consumer,
        approved_by="usr-assistant",
    )
    assert second["objects_reused"] == 1
    args = SimpleNamespace(
        pool_root=artifact_input["pool"],
        domain="usr-home",
        generation=first["generation"],
    )
    generations.switch(args)
    # The retirement retention pin protects restoration through GC first.
    maintenance.gc(SimpleNamespace(pool_root=args.pool_root, keep_generations=0))
    assert Path(first["manifest"]).exists()
    generations.retire_generation(args)
    result = maintenance.gc(
        SimpleNamespace(pool_root=args.pool_root, keep_generations=0)
    )
    assert not Path(first["manifest"]).exists()
    assert Path(second["manifest"]).exists()
    metadata = args.pool_root / "objects/metadata"
    assert (metadata / (Path(artifact_input["common"]).name + ".json")).exists()
    assert (metadata / (Path(other_system).name + ".json")).exists()
    assert not (metadata / (Path(artifact_input["system"]).name + ".json")).exists()
    assert result["retained_generations"]


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
            approved_by="usr-assistant",
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


def incremental_exchange(case, transform=lambda data: data, expected_identity=None):
    """Exercise the inventory/response/payload handshake over real duplex I/O."""
    client, server = socket.socketpair()
    client.settimeout(5)
    server.settimeout(5)
    request = None
    with (
        client,
        server,
        client.makefile("rwb") as sender,
        server.makefile("rwb") as receiver,
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                artifacts.receive_incremental,
                receiver,
                receiver,
                pool_root=case["pool"],
                expected_domain="usr-home",
                approved_by="usr-assistant",
                expected_identity=expected_identity,
            )

            def negotiate(inventory):
                nonlocal request
                artifacts.write_message(sender, inventory)
                request = artifacts.read_message(sender)
                return request

            try:
                payload = io.BytesIO()
                artifacts.write_artifact(payload, negotiate=negotiate, **case["kwargs"])
                sender.write(transform(payload.getvalue()))
                sender.flush()
            finally:
                client.shutdown(socket.SHUT_WR)
            return pending.result(timeout=5), request, payload.getvalue()


def candidate_inventory(case):
    with tarfile.open(fileobj=io.BytesIO(case["bytes"]), mode="r:*") as source:
        result = json.load(source.extractfile("manifest.json"))
    result["schema"] = artifacts.SCHEMA_INCREMENTAL
    return result


def test_candidate_bound_incremental_admission_is_inactive_and_retryable(artifact_input):
    identity = candidate_inventory(artifact_input)
    first, _, _ = incremental_exchange(artifact_input, expected_identity=identity)
    second, _, _ = incremental_exchange(artifact_input, expected_identity=identity)
    assert first["activated"] is False
    assert first["retained"] is True
    assert second["objects_transferred"] == 0
    assert second["system"] == identity["system"]
    assert second["root_artifact"] == identity["root_artifact"]


@pytest.mark.parametrize("probe,field", [
    (probe, field) for probe in (False, True)
    for field in ("domain", "system", "closure_source", "root_source", "kernel_name",
                  "source_revision", "lock_hash", "root_artifact")
    if not probe or field != "root_artifact"
])
def test_candidate_rejects_artifact_substitution_before_touching_pool(artifact_input, monkeypatch, field, probe):
    identity = candidate_inventory(artifact_input)
    inventory = dict(identity)
    inventory["schema"] = artifacts.SCHEMA_PROBE if probe else artifacts.SCHEMA_INCREMENTAL
    inventory[field] = "substituted"
    monkeypatch.setattr(artifacts, "ensure_pool", lambda *_: pytest.fail("rejected artifact touched pool"))
    with pytest.raises(core.EngineError, match="prepared candidate"):
        artifacts.receive_incremental(io.BytesIO((json.dumps(inventory) + "\n").encode()), io.BytesIO(),
            pool_root=artifact_input["pool"], expected_domain="usr-home", approved_by="sys-ctrl",
            expected_identity=identity)


def test_candidate_probe_cannot_bypass_next_inventory_check(artifact_input):
    identity = candidate_inventory(artifact_input)
    probe = dict(identity, schema=artifacts.SCHEMA_PROBE)
    inventory = dict(identity, system="substituted")
    wire = "\n".join(json.dumps(value) for value in (probe, inventory)) + "\n"
    with pytest.raises(core.EngineError, match="prepared candidate"):
        artifacts.receive_incremental(io.BytesIO(wire.encode()), io.BytesIO(),
            pool_root=artifact_input["pool"], expected_domain="usr-home", approved_by="sys-ctrl",
            expected_identity=identity)
    assert not list((artifact_input["pool"] / "domains").glob("*/generations/*/manifest.json"))


@pytest.mark.parametrize("substitute,blocked", [(False, False), (True, False), (False, True)])
def test_installed_candidate_cli_returns_one_pool_protocol_message(artifact_input, monkeypatch, substitute, blocked):
    sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))
    from airlock_machine_deploy import cli
    from airlock_machine_deploy.admission import AdmissionManager

    artifacts.receive_artifact(io.BytesIO(artifact_input["bytes"]),
        pool_root=artifact_input["pool"], expected_domain="usr-home", approved_by="sys-ctrl")
    identity = candidate_inventory(artifact_input)
    probe = dict(identity, schema=artifacts.SCHEMA_PROBE)
    if substitute:
        probe["system"] = "/nix/store/" + "d" * 32 + "-other-system"
    incoming = io.TextIOWrapper(io.BytesIO((json.dumps(probe) + "\n").encode()))
    output = io.BytesIO()
    outgoing = io.TextIOWrapper(output)
    @contextmanager
    def locked(*_args, **_kwargs):
        if blocked:
            from airlock_machine_deploy.common import DeploymentBusy
            from airlock_state import LockBusy
            raise DeploymentBusy(LockBusy({"owner": "Automatic maintenance", "reason": "Collect unused artifacts"}))
        yield
    monkeypatch.setattr(cli, "Coordinator", lambda: SimpleNamespace(locked=locked))
    monkeypatch.setattr(cli, "boot_id", lambda: "boot")
    monkeypatch.setattr(AdmissionManager, "identity", lambda *_args: identity)
    original = artifacts.receive_incremental
    def receive_cli(*args, **kwargs):
        kwargs["pool_root"] = artifact_input["pool"]
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts, "receive_incremental", receive_cli)
    with monkeypatch.context() as streams:
        streams.setattr(cli.sys, "stdin", incoming)
        streams.setattr(cli.sys, "stdout", outgoing)
        status = cli.main(["admission-receive", "a" * 64 + ".usr-home", "--caller", "sys-ctrl"])
        outgoing.flush()
    wire = io.BytesIO(output.getvalue())
    response = artifacts.read_message(wire)
    assert wire.read() == b""
    if blocked:
        assert status == 1
        assert response["schema"] == artifacts.SCHEMA_ERROR
        assert "Automatic maintenance" in response["error"]
    elif substitute:
        assert status == 1
        assert response["schema"] == artifacts.SCHEMA_ERROR
        assert "prepared candidate" in response["error"]
    else:
        assert status == 0
        assert response["generation_reused"] is True
        assert response["activated"] is False


def test_incremental_empty_pool_then_no_payload_on_repeat(artifact_input, monkeypatch):
    first, request, _ = incremental_exchange(artifact_input)
    assert request["missing"] == [artifact_input["common"], artifact_input["system"]]
    assert first["objects_transferred"] == first["objects_admitted"] == 2
    assert first["payload_bytes_transferred"] > 0
    assert request["missing_root"] is True
    assert first["root_payload_bytes_transferred"] < first["root_logical_bytes"]
    assert first["activated"] is False
    assert first["retained"] is True
    monkeypatch.setattr(
        artifacts,
        "create_tar_fragment",
        lambda *args: pytest.fail("reused objects must not be packaged"),
    )
    second, request, payload = incremental_exchange(artifact_input)
    assert second["generation"] == first["generation"]
    assert request["missing"] == []
    assert request["missing_root"] is False
    assert second["root_payload_bytes_transferred"] == 0
    assert second["root_reused"] is True
    assert second["objects_transferred"] == second["objects_admitted"] == 0
    assert second["payload_bytes_transferred"] == 0
    assert second["objects_reused"] == 2
    assert second["current_generation"] is None
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        assert archive.getnames() == ["manifest.json"]


def test_changed_root_transfers_without_resending_packages_and_survives_rollback(artifact_input):
    from airlock_qubes_storage._engine.root_images import reusable_root

    first, _, _ = incremental_exchange(artifact_input)
    kwargs = artifact_input["kwargs"]
    image = kwargs["physical_store_root"] / ("e" * 32 + "-root") / "root.img"
    image.parent.mkdir()
    with image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(1080)
        stream.write(b"\x53\xef")
        stream.seek(4096)
        stream.write(b"changed boot code")
    kwargs["root_source"] = "/nix/store/" + image.parent.name + "/root.img"
    second, request, _ = incremental_exchange(artifact_input)
    assert request["missing"] == []
    assert request["missing_root"] is True
    assert second["payload_bytes_transferred"] == 0
    assert second["root_payload_bytes_transferred"] > 0
    assert second["root_artifact"] != first["root_artifact"]
    args = SimpleNamespace(pool_root=artifact_input["pool"], domain="usr-home",
                           generation=second["generation"])
    generations.switch(args)
    args.generation = first["generation"]
    generations.rollback(args)
    maintenance.gc(SimpleNamespace(pool_root=args.pool_root, keep_generations=0))
    assert reusable_root(args.pool_root, first["root_artifact"]).exists()
    assert reusable_root(args.pool_root, second["root_artifact"]).exists()


def test_corrupt_root_payload_cannot_publish_generation(artifact_input):
    def corrupt(data):
        return rewrite_artifact(data, lambda members: [
            (name, payload[:-1] if name == "root.sparse" else payload)
            for name, payload in members
        ])

    with pytest.raises(core.EngineError, match="root artifact payload checksum"):
        incremental_exchange(artifact_input, corrupt)
    assert not list((artifact_input["pool"] / "domains").glob("*/generations/*/manifest.json"))
    result, _, _ = incremental_exchange(artifact_input)
    assert result["status"] == "passed"


def test_nested_path_order_matches_existing_tree_hash(artifact_input):
    source = (
        artifact_input["kwargs"]["physical_store_root"]
        / Path(artifact_input["system"]).name
    )
    (source / "etc/pam").mkdir(parents=True)
    (source / "etc/pam/environment").write_text("environment")
    (source / "etc/pam.d").mkdir()
    (source / "etc/pam.d/login").symlink_to("../pam/environment")
    paths = [p.relative_to(source).as_posix() for p in sorted(source.rglob("*"))]
    assert paths != sorted(paths)  # The ordering that broke live /etc admission.
    expected = core.tree_hash(source)
    first, _, _ = incremental_exchange(artifact_input)
    assert first["objects_admitted"] == 2
    metadata = core.read_json(
        artifact_input["pool"] / "objects/metadata" / (source.name + ".json")
    )
    assert metadata["tree_hash"] == expected
    second, request, _ = incremental_exchange(artifact_input)
    assert request["missing"] == []
    assert second["objects_reused"] == 2


@pytest.mark.parametrize("phase", ["inventory", "payload"])
def test_receiver_cli_reports_errors_over_protocol(
    artifact_input, monkeypatch, capsys, phase
):
    from airlock_qubes_storage import artifact_cli

    output = io.BytesIO()
    monkeypatch.setattr(artifact_cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(artifact_cli.sys, "stdout", SimpleNamespace(buffer=output))

    def reject(stream, output, **kwargs):
        if phase == "payload":
            artifacts.write_message(
                output, {"schema": artifacts.SCHEMA_REQUEST, "missing": []}
            )
        raise core.EngineError("pool artifact tree checksum mismatch: example")

    monkeypatch.setattr(artifact_cli, "receive_incremental", reject)
    assert (
        artifact_cli.main(
            [
                "receive-incremental",
                "--expected-domain",
                "usr-home",
                "--approved-by",
                "usr-assistant",
                "--pool-root",
                str(artifact_input["pool"]),
            ]
        )
        == 1
    )
    output.seek(0)
    if phase == "payload":
        assert artifacts.read_message(output)["schema"] == artifacts.SCHEMA_REQUEST
    with pytest.raises(
        core.EngineError,
        match="pool receiver rejected admission: pool artifact tree checksum mismatch",
    ):
        artifact_cli.read_receiver_message(output)
    assert "tree checksum mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["inventory", "payload"])
def test_sender_cli_reports_receiver_rejection(monkeypatch, capsys, phase):
    from airlock_qubes_storage import artifact_cli

    responses = io.BytesIO()
    if phase == "payload":
        artifacts.write_message(
            responses, {"schema": artifacts.SCHEMA_REQUEST, "missing": []}
        )
    artifacts.write_message(
        responses,
        {
            "schema": artifacts.SCHEMA_ERROR,
            "error": "pool artifact tree checksum mismatch: example",
        },
    )
    responses.seek(0)
    process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=responses,
        poll=lambda: 1,
        wait=lambda: 1,
    )
    monkeypatch.setattr(
        artifact_cli.subprocess, "Popen", lambda *args, **kwargs: process
    )
    monkeypatch.setattr(
        artifact_cli,
        "write_artifact",
        lambda output, negotiate, **kwargs: negotiate({}),
    )
    assert (
        artifact_cli.send_main(
            [
                "--domain",
                "usr-home",
                "--system",
                "unused",
                "--closure-source",
                "unused",
                "--source-revision",
                "1" * 40,
                "--lock-hash",
                "2" * 64,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "pool receiver rejected admission: pool artifact tree checksum mismatch: example"
        in captured.err
    )
    assert "missing, truncated" not in captured.err


def test_incremental_reuses_existing_pool_and_sends_only_new_object(
    artifact_input, monkeypatch
):
    # Seed through the full-artifact path, then negotiate reuse.
    receive(artifact_input)
    new = "/nix/store/" + "d" * 32 + "-new-system"
    kwargs = artifact_input["kwargs"]
    source = kwargs["physical_store_root"] / Path(new).name
    source.mkdir()
    (source / "payload").write_text("new-system-content")
    with (kwargs["closure_source"] / "store-paths").open("a") as stream:
        stream.write(new + "\n")
    kwargs["system"] = new
    kwargs["path_info"][new] = {
        "references": [artifact_input["system"]],
        "narHash": "sha256-new",
        "narSize": 3,
    }
    packaged = []
    original = artifacts.create_tar_fragment

    def track(source, entry, fragment):
        packaged.append(entry)
        return original(source, entry, fragment)

    monkeypatch.setattr(artifacts, "create_tar_fragment", track)
    result, request, _ = incremental_exchange(artifact_input)
    assert request["missing"] == [new]
    assert packaged == [Path(new).name]
    assert result["objects_reused"] == 2
    assert result["objects_admitted"] == result["objects_transferred"] == 1


@pytest.mark.parametrize("damage", ["payload", "missing", "inventory", "truncated"])
def test_incremental_rejects_bad_payload_without_publication(artifact_input, damage):
    def transform(data):
        if damage == "payload":
            return data.replace(b"common-content", b"broken-content", 1)
        if damage == "truncated":
            return data[: len(data) // 2]
        if damage == "missing":
            return rewrite_artifact(
                data,
                lambda members: [
                    item for item in members if not item[0].endswith("-common.tar")
                ],
            )

        def change_manifest(members):
            result = []
            for name, payload in members:
                if name == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["source_revision"] = "f" * 40
                    payload = json.dumps(manifest).encode()
                result.append((name, payload))
            return result

        return rewrite_artifact(data, change_manifest)

    with pytest.raises(core.EngineError):
        incremental_exchange(artifact_input, transform)
    assert not list((artifact_input["pool"] / "domains").glob("*/*/*/manifest.json"))
    assert not list((artifact_input["pool"] / "tmp").iterdir())
    result, _, _ = incremental_exchange(artifact_input)
    assert result["status"] == "passed"


def test_inventory_hash_conflict_rejected_before_request(artifact_input):
    receive(artifact_input)
    with tarfile.open(fileobj=io.BytesIO(artifact_input["bytes"])) as archive:
        inventory = json.load(archive.extractfile("manifest.json"))
    inventory["schema"] = artifacts.SCHEMA_INCREMENTAL
    inventory["objects"][0]["tree_hash"] = "0" * 64
    output = io.BytesIO()
    with pytest.raises(core.EngineError, match="existing object hash mismatch"):
        artifacts.receive_incremental(
            io.BytesIO(json.dumps(inventory).encode() + b"\n"),
            output,
            pool_root=artifact_input["pool"],
            expected_domain="usr-home",
            approved_by="usr-assistant",
        )
    assert output.getvalue() == b""


@pytest.mark.parametrize(
    "missing",
    [["/nix/store/" + "f" * 32 + "-unlisted"], [None], ["duplicate", "duplicate"]],
)
def test_sender_rejects_invalid_missing_request(artifact_input, missing):
    output = io.BytesIO()
    with pytest.raises(core.EngineError, match="invalid missing-object request"):
        artifacts.write_artifact(
            output,
            **artifact_input["kwargs"],
            negotiate=lambda _: {
                "schema": artifacts.SCHEMA_REQUEST,
                "missing": missing,
            },
        )
    assert output.getvalue() == b""


@pytest.mark.parametrize("command", ["gc", "compact", "recover"])
def test_maintenance_serializes_with_negotiated_admission(
    artifact_input, monkeypatch, command
):
    from airlock_qubes_storage._engine import cli

    locked = False

    @contextmanager
    def lock(root):
        nonlocal locked
        locked = True
        yield
        locked = False

    def maintenance(args):
        assert locked
        return {"status": "passed"}

    monkeypatch.setattr(cli, "admission_lock", lock)
    monkeypatch.setattr(cli, command, maintenance)
    assert (
        cli.run([command, "--pool-root", str(artifact_input["pool"])])["status"]
        == "passed"
    )
    assert not locked


@pytest.mark.parametrize("candidate", [None, "a" * 64])
def test_sender_cli_negotiates_and_prints_only_final_response(
    artifact_input, monkeypatch, capsys, tmp_path, candidate
):
    from airlock_qubes_storage import artifact_cli

    seeded = receive(artifact_input)
    generations.switch(
        SimpleNamespace(
            pool_root=artifact_input["pool"],
            domain="usr-home",
            generation=seeded["generation"],
        )
    )
    client, server = socket.socketpair()
    client.settimeout(5)
    server.settimeout(5)
    kwargs = artifact_input["kwargs"]
    info = tmp_path / "path-info.json"
    info.write_text(json.dumps(kwargs["path_info"]))
    with (
        client,
        server,
        server.makefile("rwb") as receiver,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):

        def receive_session():
            result = artifacts.receive_incremental(
                receiver,
                receiver,
                pool_root=artifact_input["pool"],
                expected_domain="usr-home",
                approved_by="usr-assistant",
            )
            artifacts.write_message(receiver, result)
            return 0

        future = executor.submit(receive_session)
        with client.makefile("wb") as stdin, client.makefile("rb") as stdout:

            def close_input():
                stdin.close()
                client.shutdown(socket.SHUT_WR)

            def popen(argv, **options):
                assert argv == [
                    "qrexec-client-vm",
                    "dom0",
                    (f"airlock.Pool.AdmitCandidate+{candidate}.usr-home" if candidate
                     else "airlock.Pool.AdmitIncremental+usr-home"),
                ]
                return SimpleNamespace(
                    stdin=SimpleNamespace(
                        write=stdin.write, flush=stdin.flush, close=close_input
                    ),
                    stdout=stdout,
                    wait=lambda: future.result(timeout=5),
                    poll=lambda: 0 if future.done() else None,
                    terminate=lambda: client.shutdown(socket.SHUT_RDWR),
                )

            monkeypatch.setattr(artifact_cli.subprocess, "Popen", popen)
            result = artifact_cli.send_main(
                (["--candidate-admission", candidate] if candidate else []) + [
                    "--domain",
                    "usr-home",
                    "--system",
                    kwargs["system"],
                    "--closure-source",
                    str(kwargs["closure_source"]),
                    "--source-revision",
                    kwargs["source_revision"],
                    "--lock-hash",
                    kwargs["lock_hash"],
                    "--physical-store-root",
                    str(kwargs["physical_store_root"]),
                    "--path-info-json",
                    str(info),
                ]
            )
    assert result == 0
    response = json.loads(capsys.readouterr().out)
    assert response["objects_transferred"] == 0
    assert response["objects_reused"] == 2
    assert response["activated"] is False


def test_reused_packages_are_not_read_before_or_after_negotiation(artifact_input, monkeypatch):
    receive(artifact_input)
    monkeypatch.setattr(artifacts, 'tree_hash', lambda *_: pytest.fail('existing packages were read'))
    result, request, _ = incremental_exchange(artifact_input)
    assert result['objects_transferred'] == 0
    assert set(request['reused_tree_hashes']) == {artifact_input['common'], artifact_input['system']}


def generation_probe(case, **changes):
    probe = {key: str(case['kwargs'][key]) for key in
             ('domain', 'system', 'closure_source', 'root_source', 'kernel_name', 'source_revision', 'lock_hash')}
    probe.update(schema=artifacts.SCHEMA_PROBE, source_revision='f' * 40)
    probe.update(changes)
    return probe


def test_unchanged_generation_reuses_original_provenance_without_inventory(artifact_input, monkeypatch):
    first = receive(artifact_input)
    monkeypatch.setattr(artifacts, '_receive_members', lambda *_a, **_k: pytest.fail('payload read'))
    result = artifacts.receive_incremental(io.BytesIO(json.dumps(generation_probe(artifact_input)).encode()+b'\n'),
        io.BytesIO(), pool_root=artifact_input['pool'], expected_domain='usr-home', approved_by='usr-assistant')
    assert result['generation'] == first['generation']
    assert result['source_revision'] == '1' * 40
    assert result['generation_reused'] is True
    assert result['objects_transferred'] == 0
    assert len(list((artifact_input['pool'] / 'domains/usr-home/generations').iterdir())) == 1


@pytest.mark.parametrize('field', ['system', 'closure_source', 'root_source', 'kernel_name'])
def test_changed_guest_identity_cannot_reuse_generation(artifact_input, field):
    receive(artifact_input)
    probe = generation_probe(artifact_input)
    probe[field] += '-changed'
    assert artifacts.reuse_generation(artifact_input['pool'], probe, 'usr-home', 'usr-assistant') is None


@pytest.mark.parametrize('damage', ['metadata', 'missing-map', 'unretained', 'authority'])
def test_generation_reuse_rejects_damaged_or_unapproved_state(artifact_input, damage):
    first = receive(artifact_input)
    manifest_path = Path(first['manifest'])
    manifest = core.read_json(manifest_path)
    if damage == 'metadata':
        Path(manifest['store']['canonical_metadata_image']).write_bytes(b'corrupt')
    elif damage == 'missing-map':
        Path(manifest['store']['canonical_blob_map']).unlink()
    elif damage == 'unretained':
        manifest['retention']['retain'] = False
    else:
        manifest['approval']['approved_by'] = 'other'
    core.atomic_json(manifest_path, manifest)
    if damage in {'metadata', 'missing-map'}:
        with pytest.raises((core.EngineError, FileNotFoundError)):
            artifacts.reuse_generation(artifact_input['pool'], generation_probe(artifact_input), 'usr-home', 'usr-assistant')
    else:
        assert artifacts.reuse_generation(artifact_input['pool'], generation_probe(artifact_input), 'usr-home', 'usr-assistant') is None


def test_sender_rejects_incomplete_reuse_identities(artifact_input):
    with pytest.raises(core.EngineError, match='reused-object identities'):
        artifacts.write_artifact(io.BytesIO(), **artifact_input['kwargs'], negotiate=lambda _: {
            'schema': artifacts.SCHEMA_REQUEST, 'missing': [], 'missing_root': False, 'reused_tree_hashes': {}})


def test_reuse_checks_extent_identity_and_accepts_compaction_locations(artifact_input):
    from airlock_qubes_storage._engine import views, segments
    receipt = receive(artifact_input)
    manifest = core.read_json(receipt['manifest'])
    root = artifact_input['pool']
    entry = Path(artifact_input['common']).name
    meta_path = segments.object_blob_meta_path(root, entry)
    meta = core.read_json(meta_path)
    location = segments.current_location_from_meta(meta, entry)
    blob_map = {'segments': [{'id': location['segment_id'], 'blob': str(segments.segment_blob_path(root, location['segment_id']))}],
        'logical_size': location['size'], 'extents': [{
        'object': entry, 'object_sha256': location['sha256'], 'logical_offset': 0,
        'segment_id': location['segment_id'], 'segment_offset': location['segment_offset'], 'length': location['size']}]}
    map_path = manifest['store']['canonical_blob_map']
    core.atomic_json(map_path, blob_map)
    manifest['store']['integrity'] = views.view_integrity(manifest['store'])
    views.verify_reusable_view(root, manifest)
    # Relocation within a backing segment preserves the logical checksum but
    # must correspond to a recorded admitted location.
    moved = dict(location, segment_offset=location['segment_offset'] + 512)
    blob_map['extents'][0]['segment_offset'] = moved['segment_offset']
    core.atomic_json(map_path, blob_map)
    with pytest.raises(core.EngineError, match='extent differs'):
        views.verify_reusable_view(root, manifest)
    meta.setdefault('locations', []).append(moved)
    core.atomic_json(meta_path, meta)
    backing = segments.segment_blob_path(root, moved['segment_id'])
    with backing.open('ab') as stream:
        stream.write(bytes(512))
    views.verify_reusable_view(root, manifest)


def test_generation_without_reuse_proof_takes_normal_admission(artifact_input):
    first = receive(artifact_input)
    manifest = core.read_json(first['manifest'])
    del manifest['store']['integrity']
    core.atomic_json(first['manifest'], manifest)
    assert artifacts.reuse_generation(artifact_input['pool'], generation_probe(artifact_input), 'usr-home', 'usr-assistant') is None
    second, _, _ = incremental_exchange(artifact_input)
    assert second['generation'] != first['generation']
    assert second['objects_transferred'] == 0
    assert core.read_json(second['manifest'])['store']['integrity']
