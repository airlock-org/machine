"""Shared boot-menu identity, source labels and systemd-boot ordering.

Only native entries are rewritten here. Retained copies are formatted before
host_artifacts seals them, and remain independently verifiable afterwards.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

GENERATION = re.compile(r"^Generation ([0-9]+)(?:\s|$)")
MAX_GENERATION = 2**64 - 1


def fields(text):
    result = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


def identity(text):
    return fields("\n".join(line[2:] for line in text.splitlines()
                            if line.startswith("# airlock-")))


def generation(text):
    metadata = identity(text)
    if "airlock-generation" in metadata:
        number = int(metadata["airlock-generation"])
    else:
        # Compatibility discovery for upstream and previously sealed entries.
        match = GENERATION.match(fields(text).get("version", ""))
        if not match:
            return None
        number = int(match.group(1))
    if not 0 < number <= MAX_GENERATION:
        raise ValueError("invalid boot-menu generation")
    return number


def source_metadata(system):
    path = Path(system) / "etc/airlock/boot-source.json"
    return json.loads(path.read_text()) if path.is_file() else None


def label(source):
    revision, timestamp = source["revision"], source["timestamp"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or type(timestamp) is not int or timestamp <= 0:
        raise ValueError("boot source requires a committed revision and timestamp")
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d_%H:%M") + "-" + revision[:7]


def format_entry(text, number, role, system, source=None):
    if role not in ("normal", "recovery") or not 0 < number <= MAX_GENERATION:
        raise ValueError("invalid Airlock boot identity")
    old = identity(text)
    if source is None and "airlock-revision" in old:
        source = {"revision": old["airlock-revision"], "timestamp": int(old["airlock-timestamp"])}
    # Older releases did not embed provenance. Never mislabel them with the
    # currently running or newly deployed release's identity.
    name = label(source) if source else f"legacy generation {number}"
    title = f"Airlock ({'recovery, ' if role == 'recovery' else ''}{name})"
    group = "00-airlock" if role == "normal" else "01-airlock-recovery"
    # sort-key is ascending; a fixed-width complement makes generation order
    # descending without depending on source labels or filename conventions.
    sort_key = f"{group}-{MAX_GENERATION - number:020d}"
    lines = [line for line in text.splitlines()
             if not line.startswith(("title ", "version ", "sort-key ", "machine-id ", "# airlock-"))]
    result = [f"title {title}", f"version {number}", f"sort-key {sort_key}", *lines,
              f"# airlock-generation {number}", f"# airlock-role {role}", f"# airlock-system {system}"]
    if source:
        result += [f"# airlock-revision {source['revision']}", f"# airlock-timestamp {source['timestamp']}"]
    return "\n".join(result) + "\n"


def newest_normal(entries_dir):
    choices = []
    for path in Path(entries_dir).glob("*.conf"):
        text = path.read_text()
        if identity(text).get("airlock-role") == "normal":
            choices.append((generation(text), path.name.startswith("airlock-"), path.name))
    if not choices:
        raise ValueError("no Xen Airlock normal boot entry was generated")
    return max(choices)[2]


def clear_saved_default(efivars=Path("/sys/firmware/efi/efivars"), bootctl="bootctl"):
    # Xen and image creation commonly have no EFI variable access. The file
    # default is sufficient there. Clear an existing firmware preference when
    # available, since it overrides loader.conf even without @saved.
    for variable, command in (("LoaderEntryDefault", "set-default"),
                              ("LoaderEntryPreferred", "set-preferred")):
        if (efivars / f"{variable}-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f").exists():
            subprocess.run([bootctl, command, ""], check=True)
