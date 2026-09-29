"""Exact installed Xen EFI artifacts, usable from independent recovery."""
from pathlib import Path
import os
import struct
import shutil

from ._engine import core


def xen_config(binary):
    """Read the .config PE section embedded by the upstream Xen builder."""
    data = Path(binary).read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        core.fail("invalid Xen EFI DOS header")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset + 24 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        core.fail("invalid Xen EFI PE header")
    count = struct.unpack_from("<H", data, offset + 6)[0]
    optional_size = struct.unpack_from("<H", data, offset + 20)[0]
    table = offset + 24 + optional_size
    if table + count * 40 > len(data):
        core.fail("truncated Xen EFI section table")
    sections = []
    for index in range(count):
        section = table + index * 40
        if data[section:section + 8].rstrip(b"\0") != b".config":
            continue
        size, start = struct.unpack_from("<II", data, section + 16)
        if not size or start + size > len(data):
            core.fail("truncated Xen EFI configuration")
        sections.append(data[start:start + size].rstrip(b"\0").decode("utf-8"))
    if len(sections) != 1:
        core.fail("Xen EFI must contain exactly one configuration section")
    return dict(line.split("=", 1) for line in sections[0].splitlines() if "=" in line)


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
        config = xen_config(binary)
        kernel = config.get("kernel", "").split()
        if f"init={system}/init" not in kernel:
            continue
        names = [kernel[0], config["ramdisk"]]
        if any(Path(name).name != name for name in names):
            core.fail("Xen boot artifact escapes its EFI directory")
        files = [entry, binary, *(binary.parent / name for name in names)]
        fingerprints = {}
        for path in files:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            fingerprints[str(path.relative_to(esp))] = {"size": path.stat().st_size, "sha256": core.hash_file(path)}
        entries.append({"entry": entry.name, "files": fingerprints})
    return entries


def seal(root, esp):
    root = Path(root)
    esp = Path(esp)
    for path in (root / "host/boots").glob("*.json"):
        record = core.read_json(path)
        if record.get("retired"):
            continue
        descriptor = root / "host/artifacts" / path.name
        if descriptor.exists():
            # A retained entry must survive upstream menu regeneration even
            # when its system path is absent from the currently mounted view.
            selected = verify(root, esp, record)
            remove_native_duplicates(esp, record["system"], selected)
            continue
        entries = inventory(esp, record["system"])
        if entries:
            retained = []
            for entry in entries:
                files = {}
                source_entry = esp / "loader/entries" / entry["entry"]
                name = "airlock-" + entry["entry"]
                for relative, fingerprint in entry["files"].items():
                    source = esp / relative
                    if source == source_entry:
                        continue
                    target = esp / "EFI/airlock" / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if core.hash_file(target) != fingerprint["sha256"]:
                            core.fail("retained EFI filename collision")
                    else:
                        temporary = target.with_name(target.name + ".pending")
                        shutil.copyfile(source, temporary)
                        with temporary.open("rb") as stream:
                            os.fsync(stream.fileno())
                        temporary.replace(target)
                        core.fsync_dir(target.parent)
                    files[str(target.relative_to(esp))] = fingerprint
                target_entry = esp / "loader/entries" / name
                text = source_entry.read_text().replace("/EFI/nixos/", "/EFI/airlock/").rstrip("\n") + "\n"
                role = "recovery" if "title Airlock (recovery)" in text else "normal"
                # Menu classification must not open an unmounted old store.
                # Comments are ignored by systemd-boot and covered by our hash.
                text += f"# airlock-system {record['system']}\n# airlock-role {role}\n"
                lines = text.splitlines()
                version = next((line.removeprefix("version ") for line in lines if line.startswith("version ")), "")
                parts = version.split()
                generation = parts[1] if len(parts) > 1 and parts[0] == "Generation" and parts[1].isdigit() else str(record["generation"])
                # systemd-boot adds versions only to distinguish equal titles.
                # Keep those versions short; a fresh image has unique titles.
                title = "Airlock (recovery)" if role == "recovery" else "Airlock"
                lines = [line for line in lines if not line.startswith(("title ", "version "))]
                text = f"title {title}\nversion Generation {generation}\n" + "\n".join(lines) + "\n"
                core.atomic_write(target_entry, text)
                files[str(target_entry.relative_to(esp))] = {
                    "size": target_entry.stat().st_size, "sha256": core.hash_file(target_entry)}
                retained.append({"entry": name, "files": files})
            core.atomic_json(descriptor,
                             {"schema": "airlock.host-boot-artifacts.v1", "system": record["system"],
                              "generation": record["generation"], "entries": retained})
            selected = verify(root, esp, record)
            remove_native_duplicates(esp, record["system"], selected)


def remove_native_duplicates(esp, system, selected):
    """Retire generated menu copies only after their independent copy verifies."""
    esp = Path(esp)
    entries = inventory(esp, system)
    names = {entry["entry"].removesuffix(".conf") for entry in entries}
    loader = esp / "loader/loader.conf"
    if names and loader.exists():
        lines = loader.read_text().splitlines()
        for index, line in enumerate(lines):
            fields = line.split()
            if len(fields) == 2 and fields[0] in ("default", "preferred") and fields[1].removesuffix(".conf") in names:
                lines[index] = f"{fields[0]} {selected.removesuffix('.conf')}"
        # Commit the new default before removing any formerly selected entry.
        core.atomic_write(loader, "\n".join(lines) + "\n")
    for entry in entries:
        (esp / "loader/entries" / entry["entry"]).unlink()
    if entries:
        core.fsync_dir(esp / "loader/entries")


def forget(root, esp, system):
    """Remove only retired entry files not referenced by another descriptor."""
    directory = Path(root) / "host/artifacts"
    descriptor = directory / (core.store_entry(system) + ".json")
    if not descriptor.exists():
        return
    retained = set()
    for path in directory.glob("*.json"):
        if path != descriptor:
            for entry in core.read_json(path)["entries"]:
                retained.update(entry["files"])
    for entry in core.read_json(descriptor)["entries"]:
        for name in entry["files"]:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or not (
                    name.startswith("EFI/airlock/") or name.startswith("loader/entries/airlock-")):
                core.fail("invalid retained boot artifact path")
            if name not in retained:
                target = Path(esp) / relative
                target.unlink(missing_ok=True)
                if target.parent.exists():
                    core.fsync_dir(target.parent)
    descriptor.unlink()
    core.fsync_dir(directory)


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
