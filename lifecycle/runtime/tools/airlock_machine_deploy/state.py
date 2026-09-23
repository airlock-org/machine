"""Durable JSON state and release loading; no VM or Nix command execution."""

import json
import os
from pathlib import Path

from .validation import validate_release_header, validate_release_topology


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_release(path):
    path = Path(path).resolve()
    release = read_json(path / "release.json")
    validate_release_header(release)
    topology = read_json(path / "topology.json")
    validate_release_topology(release, topology)
    return release, topology
