#!/usr/bin/env python3
"""Present Airlock roles and retained normal generations in systemd-boot."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess

from airlock_qubes_storage import boot_presentation as presentation

STORE_SYSTEM = re.compile(r"(?:^|\s)init=(/nix/store/[^/\s]+)/init(?:\s|$)")


@dataclass(frozen=True)
class Entry:
    path: Path
    generation: int
    role: str
    system: Path


def fields(path):
    result = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition(" ")
        if separator:
            result[key] = value
    return result


def system_for_xen_entry(entry, store_root):
    ordinary = entry.with_name("nixos-" + entry.name.removeprefix("xen-"))
    if not ordinary.is_file():
        return None
    match = STORE_SYSTEM.search(fields(ordinary).get("options", ""))
    if not match:
        return None
    return store_root / Path(match.group(1)).name


def classified_entries(entries_dir, store_root):
    result = []
    for path in entries_dir.glob("xen-*.conf"):
        text = path.read_text()
        generation = presentation.generation(text)
        comments = presentation.identity(text)
        logical_system = comments.get("airlock-system", "")
        system = (store_root / Path(logical_system).name if logical_system.startswith("/nix/store/")
                  else system_for_xen_entry(path, store_root))
        marker = system / "etc/airlock/boot-role" if system else None
        role = comments.get("airlock-role") or (marker.read_text().strip() if marker and marker.is_file() else None)
        if generation and system and role in ("normal", "recovery"):
            result.append(Entry(path, generation, role, system))
    for path in entries_dir.glob("airlock-xen-*.conf"):
        comments = presentation.identity(path.read_text())
        generation = presentation.generation(path.read_text())
        system = comments.get("airlock-system", "")
        role = comments.get("airlock-role")
        if generation and system.startswith("/nix/store/") and role in ("normal", "recovery"):
            result.append(Entry(path, generation, role, store_root / Path(system).name))
    return result


def replace_field(path, key, value):
    lines = path.read_text().splitlines()
    replacement = f"{key} {value}"
    for index, line in enumerate(lines):
        if line.startswith(key + " "):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    os.replace(temporary, path)


def choose_one_per_generation(entries):
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry.generation].append(entry)
    selected = []
    for choices in grouped.values():
        choices.sort(
            key=lambda entry: (
                not entry.path.name.startswith("airlock-"),
                entry.path.name,
            )
        )
        selected.append(choices[0])
    return sorted(selected, key=lambda entry: entry.generation, reverse=True)


def configure(esp, store_root, recovery_profile=None, nix_env="nix-env", bootctl="bootctl"):
    entries_dir = esp / "loader/entries"
    loader_conf = esp / "loader/loader.conf"
    if not entries_dir.is_dir() or not loader_conf.is_file():
        raise ValueError("systemd-boot files are missing from the EFI partition")

    loader = fields(loader_conf)
    classified = classified_entries(entries_dir, store_root)
    normal = choose_one_per_generation([entry for entry in classified if entry.role == "normal"])
    recovery = choose_one_per_generation([entry for entry in classified if entry.role == "recovery"])
    if not normal:
        raise ValueError("no Xen Airlock normal boot entry was generated")
    if not recovery:
        raise ValueError("no Xen Airlock recovery boot entry was generated")

    current = normal[0]
    current_recovery = recovery[0]
    # Validate every native label before modifying the menu or recovery profile.
    formatted = {
        entry.path: presentation.format_entry(
            entry.path.read_text(), entry.generation, entry.role,
            "/nix/store/" + entry.system.name, presentation.source_metadata(entry.system))
        for entry in normal + recovery if not entry.path.name.startswith("airlock-")
    }
    if recovery_profile and not current_recovery.path.name.startswith("airlock-"):
        recovery_profile.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                nix_env,
                "--profile",
                str(recovery_profile),
                "--set",
                str(current_recovery.system),
            ],
            check=True,
        )
    retained = {entry.path for entry in normal + recovery}
    for entry in entries_dir.glob("*.conf"):
        if entry.name.startswith(("nixos-", "xen-")) and entry not in retained:
            entry.unlink()

    # Retained entries are checksum-bound boot artifacts. Preserve their bytes.
    for path, text in formatted.items():
        temporary = path.with_suffix(".conf.tmp")
        temporary.write_text(text)
        os.replace(temporary, path)

    presentation.clear_saved_default(bootctl=bootctl)
    replace_field(loader_conf, "default", current.path.name)
    if "preferred" in loader:
        replace_field(loader_conf, "preferred", current.path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--esp", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, default=Path("/nix/store"))
    parser.add_argument("--recovery-profile", type=Path)
    parser.add_argument("--nix-env", default="nix-env")
    parser.add_argument("--bootctl", default="bootctl")
    args = parser.parse_args()
    try:
        configure(
            args.esp,
            args.store_root,
            recovery_profile=args.recovery_profile,
            nix_env=args.nix_env,
            bootctl=args.bootctl,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
