"""Capture completion delivery: atomic publication, retries and narrow inputs."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "lifecycle/misc/modules/sys-gui/capture-transfer.py"


@pytest.fixture
def capture(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("capture_transfer", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(module, "capture_directory", lambda kind: tmp_path / kind)
    monkeypatch.setattr(module, "notify", lambda message: None)
    return module


def payload(data=b"capture contents", name="Screenshot From today.png", identity="a" * 64):
    header = {"schema": "airlock.capture.v1", "id": identity, "name": name,
              "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return header, io.BytesIO((json.dumps(header) + "\n").encode() + data)


def test_completed_capture_is_published_and_retry_is_idempotent(capture):
    _, stream = payload()
    receipt = capture.receive("image", stream)
    assert Path(receipt["path"]).read_bytes() == b"capture contents"
    _, stream = payload()
    assert capture.receive("image", stream) == receipt
    assert list(capture.capture_directory("image").iterdir()) == [Path(receipt["path"])]


def test_existing_personal_file_is_preserved(capture):
    directory = capture.capture_directory("image")
    directory.mkdir()
    original = directory / "Screenshot From today.png"
    original.write_bytes(b"personal file")
    _, stream = payload()
    receipt = capture.receive("image", stream)
    assert Path(receipt["path"]) != original
    assert original.read_bytes() == b"personal file"
    assert Path(receipt["path"]).read_bytes() == b"capture contents"


def test_replaced_received_file_is_preserved_and_capture_is_restored(capture):
    _, stream = payload()
    first = capture.receive("image", stream)
    Path(first["path"]).write_bytes(b"subsequent personal edit")
    _, stream = payload()
    second = capture.receive("image", stream)
    assert second["path"] != first["path"]
    assert Path(first["path"]).read_bytes() == b"subsequent personal edit"
    assert Path(second["path"]).read_bytes() == b"capture contents"


def test_retry_after_crash_between_publication_and_acknowledgement(capture, monkeypatch):
    real_sync = capture.sync_directory

    def crash(path):
        if path == capture.capture_directory("image"):
            raise OSError("simulated crash after publication")
        real_sync(path)

    monkeypatch.setattr(capture, "sync_directory", crash)
    _, stream = payload()
    with pytest.raises(OSError):
        capture.receive("image", stream)
    monkeypatch.setattr(capture, "sync_directory", real_sync)
    _, stream = payload()
    receipt = capture.receive("image", stream)
    assert list(capture.capture_directory("image").iterdir()) == [Path(receipt["path"])]


@pytest.mark.parametrize("case", ["truncated", "extra", "corrupt"])
def test_failed_transfer_does_not_publish_partial_capture(capture, case):
    header, _ = payload()
    data = {"truncated": b"capture", "extra": b"capture contents!",
            "corrupt": b"Capture contents"}[case]
    stream = io.BytesIO((json.dumps(header) + "\n").encode() + data)
    with pytest.raises(ValueError):
        capture.receive("image", stream)
    assert list(capture.capture_directory("image").iterdir()) == []
    assert not (capture.state_root() / "imported").exists()


@pytest.mark.parametrize("key,value", [
    ("name", "../escape.png"), ("name", "x\n.png"), ("name", "link.svg"),
    ("id", "../../escape"), ("sha256", "bad"), ("size", True),
    ("size", -1), ("schema", "unknown"),
])
def test_receiver_rejects_invalid_metadata(capture, key, value):
    header, _ = payload()
    header[key] = value
    with pytest.raises(ValueError):
        capture.receive("image", io.BytesIO((json.dumps(header) + "\n").encode()))
    assert not capture.capture_directory("image").exists()


def test_capture_source_must_be_regular_and_in_capture_directory(capture, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"capture")
    with pytest.raises(ValueError, match="directory"):
        capture.enqueue("image", str(outside))
    directory = capture.capture_directory("image")
    directory.mkdir()
    link = directory / "linked.png"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="regular"):
        capture.enqueue("image", str(link))


def test_transfer_failure_then_success_retries_without_duplicate(capture, tmp_path, monkeypatch):
    source_dir = capture.capture_directory("image")
    source_dir.mkdir()
    source = source_dir / "Screenshot From today.png"
    source.write_bytes(b"finished capture")
    identity = capture.enqueue("image", str(source))
    fake = tmp_path / "qrexec.py"
    fake.write_text(f"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('remote', {str(SOURCE)!r})
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
remote.state_root = lambda: Path({str(tmp_path / 'remote-state')!r})
remote.capture_directory = lambda kind: Path({str(tmp_path / 'remote')!r}) / kind
print(json.dumps(remote.receive(sys.argv[2].split('+')[1], sys.stdin.buffer)))
""")
    real_popen = subprocess.Popen
    attempts = []

    def transport(command, **kwargs):
        attempts.append(command)
        return real_popen([sys.executable, str(fake), *command[1:]], **kwargs)

    def fail(job, target):
        raise RuntimeError("usr-home is absent")

    real_deliver = capture.deliver
    monkeypatch.setattr(capture, "deliver", fail)
    assert not capture.drain("usr-home")
    assert source.exists()
    assert (capture.state_root() / "pending" / f"{identity}.json").exists()
    monkeypatch.setattr(capture, "deliver", real_deliver)
    monkeypatch.setattr(capture.subprocess, "Popen", transport)
    assert capture.drain("usr-home")
    assert attempts[0][1:] == ["usr-home", "airlock.CaptureImport+image"]
    assert list((tmp_path / "remote/image").iterdir())[0].read_bytes() == source.read_bytes()
    capture.enqueue("image", str(source))
    assert capture.drain("usr-home")
    assert len(attempts) == 1


def test_recording_changed_after_completion_is_not_sent(capture):
    directory = capture.capture_directory("video")
    directory.mkdir()
    source = directory / "Screencast.webm"
    source.write_bytes(b"finished")
    identity = capture.enqueue("video", str(source))
    source.write_bytes(b"still writing")
    with pytest.raises(ValueError, match="changed"):
        capture.deliver(capture.read_json(capture.state_root() / "pending" / f"{identity}.json"), "usr-home")


def test_capture_open_cannot_select_an_arbitrary_path(capture, monkeypatch):
    _, stream = payload()
    receipt = capture.receive("image", stream)
    launched = []
    monkeypatch.setattr(capture.subprocess, "run", lambda command, **kwargs: launched.append(command))
    capture.open_received("image", io.BytesIO((json.dumps({"id": receipt["id"], "action": "show"}) + "\n").encode()))
    assert "org.freedesktop.FileManager1.ShowItems" in launched[0]
    assert Path(receipt["path"]).as_uri() in launched[0][-2]
    with pytest.raises(ValueError):
        capture.open_received("video", io.BytesIO((json.dumps({"id": receipt["id"], "action": "open"}) + "\n").encode()))


def test_receiver_rejects_other_qubes_before_reading(monkeypatch, capture):
    monkeypatch.setenv("QREXEC_REMOTE_DOMAIN", "other-vm")
    monkeypatch.setattr(sys, "argv", ["capture", "receive", "image"])
    with pytest.raises(ValueError, match="only sys-gui"):
        capture.main()


def test_open_notification_does_not_enqueue_an_unfinished_video(capture, monkeypatch):
    directory = capture.capture_directory("video")
    directory.mkdir()
    path = directory / "Screencast.webm"
    path.write_bytes(b"incomplete recording")
    monkeypatch.setattr(sys, "argv", ["capture", "show", "video", str(path)])
    capture.main()
    assert not (capture.state_root() / "pending").exists()
    assert not (capture.state_root() / "sent").exists()
