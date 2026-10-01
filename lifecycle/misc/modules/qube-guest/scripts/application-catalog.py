"""Expose the active desktop environment through upstream GetAppmenus format.

Profiles, rather than the store or GC roots, select installed applications.
The first desktop ID wins, including Hidden entries that mask a lower profile.
"""

import configparser
import os
from pathlib import Path
import re
import shutil
import sys

FIELDS = {
    "Name",
    "GenericName",
    "Comment",
    "Exec",
    "Icon",
    "StartupWMClass",
    "Categories",
    "Keywords",
    "OnlyShowIn",
    "NotShowIn",
}
DESKTOP_ID = re.compile(r"[A-Za-z0-9._-]+\Z")


def application_dirs(environ):
    home = Path(environ["HOME"])
    roots = [environ.get("XDG_DATA_HOME", str(home / ".local/share"))]
    roots += environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    return list(
        dict.fromkeys(
            Path(root) / "applications" for root in roots if Path(root).is_absolute()
        )
    )


def desktop_files(directory):
    # Nix profiles can contain symlinks to whole application subdirectories.
    # Track directory inodes so cycles cannot make a scan unbounded.
    visited = set()
    def failed_scan(error):
        raise error

    for root, dirs, files in os.walk(directory, followlinks=True, onerror=failed_scan):
        stat = os.stat(root)
        identity = (stat.st_dev, stat.st_ino)
        if identity in visited:
            dirs[:] = []
            continue
        visited.add(identity)
        dirs.sort()
        for filename in sorted(files):
            if filename.endswith(".desktop"):
                path = Path(root) / filename
                yield str(path.relative_to(directory)).replace("/", "-"), path


def catalog(directories, *, persistence="full", rw=Path("/rw"), environ=None):
    environ = os.environ if environ is None else environ
    entries = {}
    seen = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        if persistence != "full":
            if (
                persistence != "rw-only"
                or not rw.exists()
                or directory.stat().st_dev != rw.stat().st_dev
            ):
                continue
        for desktop_id, path in desktop_files(directory):
            if desktop_id in seen or not DESKTOP_ID.fullmatch(desktop_id):
                continue
            seen.add(desktop_id)
            if len(seen) > 10000:
                raise ValueError("too many desktop entries")
            if path.stat().st_size > 1024 * 1024:
                raise ValueError(f"desktop entry too large: {desktop_id}")
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read_string(path.read_text(encoding="utf-8"))
            except (configparser.Error, UnicodeError):
                continue
            if not parser.has_section("Desktop Entry"):
                continue
            entry = parser["Desktop Entry"]
            if entry.get("Type", "Application") != "Application":
                continue
            if any(
                entry.get(key, "").lower() == "true" for key in ("Hidden", "NoDisplay")
            ):
                continue
            if not entry.get("Name") or (
                not entry.get("Exec")
                and entry.get("DBusActivatable", "").lower() != "true"
            ):
                continue
            if "Screensaver" in entry.get("Categories", "").split(";"):
                continue
            executable = entry.get("TryExec")
            if executable and not shutil.which(
                executable, path=environ.get("PATH", "")
            ):
                continue
            values = {
                key: value
                for key, value in entry.items()
                if key.split("[", 1)[0] in FIELDS and "\n" not in value
            }
            # No guest command is executed in the GUI VM. StartApp resolves the
            # original desktop entry inside the originating VM.
            values["Exec"] = "qubes-desktop-run " + desktop_id
            # Profile switches must invalidate icons even if Name/Icon did not
            # change. The receiver hashes this field but never publishes it.
            values["X-Airlock-Source"] = str(path.resolve())
            entries[desktop_id] = values
    return entries


def encode(entries):
    return "".join(
        f"{desktop_id}:{key}={value}\n"
        for desktop_id, values in sorted(entries.items())
        for key, value in sorted(values.items())
    )


def main():
    # Produce the complete snapshot before writing anything to qrexec. Failed
    # reads must not turn a partial application list into an uninstall event.
    entries = catalog(
        application_dirs(os.environ),
        persistence=os.environ.get("AIRLOCK_VM_PERSISTENCE", "full"),
    )
    sys.stdout.write(encode(entries))


if __name__ == "__main__":
    main()
