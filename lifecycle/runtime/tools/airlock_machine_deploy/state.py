"""Durable JSON state and release loading; no VM or Nix command execution."""

import json
from pathlib import Path

from airlock_state import atomic_json as atomic_json

from .validation import validate_release_header, validate_release_topology


def read_json(path):
    return json.loads(Path(path).read_text())


def load_release(path):
    path = Path(path).resolve()
    release = read_json(path / "release.json")
    validate_release_header(release)
    topology = read_json(path / "topology.json")
    validate_release_topology(release, topology)
    return release, topology
