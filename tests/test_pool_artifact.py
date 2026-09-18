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
        }

    monkeypatch.setattr(artifacts, "materialize_view", fake_view)
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
        approved_by="sys-dev",
    )
    assert result["root_source"] == expected_root
    assert result["kernel_name"] == expected_kernel
    assert result["previous_generation"] == first["generation"]
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
            approved_by="sys-dev",
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
    assert described["approval"]["approved_by"] == "sys-dev"
    assert described["retention"]["retain"] is True


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


def incremental_exchange(case, transform=lambda data: data):
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
                approved_by="sys-dev",
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


def test_incremental_empty_pool_then_no_payload_on_repeat(artifact_input, monkeypatch):
    first, request, _ = incremental_exchange(artifact_input)
    assert request["missing"] == [artifact_input["common"], artifact_input["system"]]
    assert first["objects_transferred"] == first["objects_admitted"] == 2
    assert first["payload_bytes_transferred"] > 0
    assert first["activated"] is False
    assert first["retained"] is True
    monkeypatch.setattr(
        artifacts,
        "create_tar_fragment",
        lambda *args: pytest.fail("reused objects must not be packaged"),
    )
    second, request, payload = incremental_exchange(artifact_input)
    assert request["missing"] == []
    assert second["objects_transferred"] == second["objects_admitted"] == 0
    assert second["payload_bytes_transferred"] == 0
    assert second["objects_reused"] == 2
    assert second["current_generation"] is None
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        assert archive.getnames() == ["manifest.json"]


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
                "sys-dev",
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
    # Seed through the existing full-artifact path: old objects need no migration.
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
            approved_by="sys-dev",
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


def test_sender_cli_negotiates_and_prints_only_final_response(
    artifact_input, monkeypatch, capsys, tmp_path
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
                approved_by="sys-dev",
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
                    "airlock.Pool.AdmitIncremental+usr-home",
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
                [
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
