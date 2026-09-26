"""Exact installed Xen EFI artifacts, usable from independent recovery."""
from pathlib import Path
import os

from ._engine import core


def inventory(esp, system):
    esp = Path(esp)
    entries = []
    for entry in sorted((esp / "loader/entries").glob("xen-*.conf")):
        fields = dict(line.split(None, 1) for line in entry.read_text().splitlines()
                      if line.strip() and not line.startswith("#") and len(line.split(None, 1)) == 2)
        efi = fields.get("efi", "")
        if not efi.startswith("/EFI/nixos/") or ".." in Path(efi).parts:
            continue
        binary = esp / efi.lstrip("/")
        configuration = binary.with_suffix(".cfg")
        if not configuration.is_file():
            continue
        config = dict(line.split("=", 1) for line in configuration.read_text().splitlines() if "=" in line)
        kernel = config.get("kernel", "").split()
        if f"init={system}/init" not in kernel:
            continue
        names = [kernel[0], config["ramdisk"]]
        if any(Path(name).name != name for name in names):
            core.fail("Xen boot artifact escapes its EFI directory")
        files = [entry, binary, configuration, *(binary.parent / name for name in names)]
        fingerprints = {}
        for path in files:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            fingerprints[str(path.relative_to(esp))] = {"size": path.stat().st_size, "sha256": core.hash_file(path)}
        entries.append({"entry": entry.name, "files": fingerprints})
    return entries


def seal(root, esp):
    root = Path(root)
    for path in (root / "host/boots").glob("*.json"):
        record = core.read_json(path)
        if record.get("retired"):
            continue
        entries = inventory(esp, record["system"])
        if entries:
            core.atomic_json(root / "host/artifacts" / path.name,
                             {"schema": "airlock.host-boot-artifacts.v1", "system": record["system"],
                              "generation": record["generation"], "entries": entries})


def verify(root, esp, record):
    descriptor = core.read_json(Path(root) / "host/artifacts" / (core.store_entry(record["system"]) + ".json"))
    if (descriptor.get("schema") != "airlock.host-boot-artifacts.v1" or
            descriptor.get("system") != record["system"] or
            descriptor.get("generation") != record["generation"] or not descriptor.get("entries")):
        core.fail("host boot artifact binding mismatch")
    for entry in descriptor["entries"]:
        for name, expected in entry["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                core.fail("invalid boot artifact path")
            path = Path(esp) / name
            if not path.is_file() or path.stat().st_size != expected["size"] or core.hash_file(path) != expected["sha256"]:
                core.fail(f"missing or corrupt boot artifact: {name}")
    return descriptor["entries"][0]["entry"]


def select_entry(esp, entry):
    """Use the existing loader.conf contract, including Xen without EFI vars."""
    if Path(entry).name != entry:
        core.fail("invalid loader entry name")
    path = Path(esp) / "loader/loader.conf"
    lines = [line for line in path.read_text().splitlines()
             if not line.split() or line.split()[0] != "default"]
    core.atomic_write(path, "\n".join([f"default {entry}", *lines]) + "\n")
