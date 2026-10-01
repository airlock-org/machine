"""Resolve icons in the same active profiles as GetAppmenus and StartApp."""

import os
from pathlib import Path
import sys

import xdg.IconTheme


def main():
    name, size = sys.argv[1:]
    themes = ["Adwaita", "hicolor", "gnome", "oxygen"]
    roots = [os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))]
    roots += os.environ.get("XDG_DATA_DIRS", "").split(":")
    for root in roots:
        directory = Path(root) / "icons"
        if directory.is_dir():
            themes.extend(
                path.name for path in sorted(directory.iterdir()) if path.is_dir()
            )
    for theme in dict.fromkeys(themes):
        icon = xdg.IconTheme.getIconPath(name, size=int(size), theme=theme)
        if icon:
            print(icon)
            return
    raise SystemExit(1)


if __name__ == "__main__":
    main()
