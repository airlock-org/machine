"""Canonical sparse encoding of an immutable ext4 boot image (stdlib only)."""

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys

SCHEMA = "airlock.root-artifact.v1"
MAGIC = b"AIRLOCK-ROOT-SPARSE-1\n"
BLOCK = 65536
MAX_SIZE = 64 * 1024**3
FORMAT = "ext4"
BOOT_ABI = "airlock.nix-closure-root.v1"


def validate_descriptor(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "sha256", "logical_bytes", "payload_bytes", "format", "boot_abi"
    }:
        raise ValueError("invalid root artifact descriptor")
    if (value["schema"] != SCHEMA or value["format"] != FORMAT
            or value["boot_abi"] != BOOT_ABI
            or not isinstance(value["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or type(value["logical_bytes"]) is not int
            or not 0 < value["logical_bytes"] <= MAX_SIZE
            or type(value["payload_bytes"]) is not int
            or not len(MAGIC) + 8 <= value["payload_bytes"] <=
            value["logical_bytes"] + 12 * ((value["logical_bytes"] + BLOCK - 1) // BLOCK) + len(MAGIC) + 8):
        raise ValueError("invalid root artifact identity or size")
    return value


def blocks(stream, size):
    """Skip filesystem holes, but always encode fixed, aligned nonzero blocks."""
    offset = 0
    seek_holes = hasattr(os, "SEEK_DATA")
    while offset < size:
        if seek_holes:
            try:
                data = os.lseek(stream.fileno(), offset, os.SEEK_DATA)
                offset = max(offset, data // BLOCK * BLOCK)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
                seek_holes = False
        stream.seek(offset)
        chunk = stream.read(min(BLOCK, size - offset))
        if not chunk:
            raise ValueError("truncated root image")
        if chunk.strip(b"\0"):
            yield struct.pack(">QI", offset, len(chunk)) + chunk
        offset += len(chunk)


def encode(source, target=None):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("root image must be a regular file")
    size = source.stat().st_size
    if not 0 < size <= MAX_SIZE:
        raise ValueError("root image size is out of bounds")
    digest = hashlib.sha256()
    count = 0

    def emit(chunk):
        nonlocal count
        digest.update(chunk)
        count += len(chunk)
        if target is not None:
            target.write(chunk)

    emit(MAGIC + struct.pack(">Q", size))
    with source.open("rb") as stream:
        stream.seek(1080)
        if stream.read(2) != b"\x53\xef":
            raise ValueError("root image is not an ext filesystem")
        for chunk in blocks(stream, size):
            emit(chunk)
    return {"schema": SCHEMA, "sha256": digest.hexdigest(), "logical_bytes": size,
            "payload_bytes": count, "format": FORMAT, "boot_abi": BOOT_ABI}


def decode(source, target, descriptor):
    """Create a separate sparse file; never extract a pathname from the stream."""
    validate_descriptor(descriptor)
    source = Path(source)
    if source.stat().st_size != descriptor["payload_bytes"]:
        raise ValueError("root payload size mismatch")
    digest = hashlib.sha256()
    with source.open("rb") as stream, Path(target).open("xb") as output:
        header = stream.read(len(MAGIC) + 8)
        digest.update(header)
        if (len(header) != len(MAGIC) + 8 or not header.startswith(MAGIC)
                or struct.unpack(">Q", header[-8:])[0] != descriptor["logical_bytes"]):
            raise ValueError("invalid sparse root header")
        output.truncate(descriptor["logical_bytes"])
        end = 0
        while record := stream.read(12):
            if len(record) != 12:
                raise ValueError("truncated sparse root record")
            offset, size = struct.unpack(">QI", record)
            if (offset < end or offset % BLOCK or offset >= descriptor["logical_bytes"]
                    or size != min(BLOCK, descriptor["logical_bytes"] - offset)):
                raise ValueError("invalid or overlapping sparse root extent")
            chunk = stream.read(size)
            if len(chunk) != size or not chunk.strip(b"\0"):
                raise ValueError("truncated or noncanonical sparse root extent")
            digest.update(record + chunk)
            output.seek(offset)
            output.write(chunk)
            end = offset + size
        if digest.hexdigest() != descriptor["sha256"]:
            raise ValueError("root artifact checksum mismatch")
        output.flush()
        os.fsync(output.fileno())
    if encode(target) != descriptor:
        raise ValueError("materialized root artifact differs from descriptor")


def write_descriptor(source, manifest):
    descriptor = encode(source)
    Path(manifest).write_text(json.dumps(descriptor, sort_keys=True) + "\n")


if __name__ == "__main__":
    write_descriptor(*sys.argv[1:])
