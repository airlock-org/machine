"""Check the packaged session helper, including its runtime dependency closure."""

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_guest_session_has_no_implicit_xfce_dependency(tmp_path):
    built = subprocess.run(
        ["nix", "build", f"{ROOT}#qubes-gui-agent-linux", "--no-link", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(built.stdout)[0]["outputs"]["out"]
    closure = subprocess.run(
        ["nix", "path-info", "--recursive", output],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert not any("-xfce4-session-" in path for path in closure)
    store_root = os.environ.get("AIRLOCK_NIX_ROOT", "")
    script = (Path(store_root + output) / "bin/qubes-session").read_text()
    assert "xfce4-session" not in script
    # Only stub the Qubes service check: execute the real GUI-session branch.
    script, count = re.subn(
        r"^\. /nix/store/[^\n]+/lib/qubes/init/functions$",
        "qsvc() { return 0; }",
        script,
        flags=re.MULTILINE,
    )
    assert count == 1
    helper = tmp_path / "qubes-session"
    helper.write_text(script)
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("XSESSION", None)
    missing = subprocess.run(
        ["bash", str(helper)], env=env, capture_output=True, text=True
    )
    assert missing.returncode == 1
    assert "requires an explicit XSESSION" in missing.stderr
    selected = tmp_path / "selected-session"
    selected.write_text("#!/usr/bin/env bash\necho explicit-session-started\n")
    selected.chmod(0o755)
    configured = subprocess.run(
        ["bash", str(helper)],
        env=dict(env, XSESSION=str(selected)),
        capture_output=True,
        text=True,
    )
    assert configured.returncode == 0
    assert configured.stdout.strip() == "explicit-session-started"
