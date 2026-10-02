"""Installed development tools, Console routing, and persistent agent guidance."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "lifecycle/misc/modules/sys-agent/workspace-init.sh"


def initialize(codex_state, instructions):
    subprocess.run(["bash", str(INIT), str(codex_state), str(instructions)], check=True)


def test_workspace_guidance_follows_generation_without_replacing_user_state(tmp_path):
    state = tmp_path / "private/codex"
    instructions = tmp_path / "system/AGENTS.md"
    instructions.parent.mkdir()
    instructions.write_text("generation one")
    initialize(state, instructions)
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "AGENTS.md").is_symlink()
    (state / "auth.json").write_text("private credentials")

    instructions.write_text("generation two")
    initialize(state, instructions)
    assert (state / "AGENTS.md").read_text() == "generation two"
    assert (state / "auth.json").read_text() == "private credentials"


@pytest.mark.parametrize("existing", ["file", "symlink", "dangling-symlink"])
def test_workspace_preserves_custom_guidance(tmp_path, existing):
    state = tmp_path / "codex"
    state.mkdir()
    custom = tmp_path / "custom.md"
    custom.write_text("user instructions")
    target = state / "AGENTS.md"
    if existing == "file":
        target.write_text("user instructions")
    else:
        target.symlink_to(custom if existing == "symlink" else tmp_path / "missing")
    initialize(state, tmp_path / "new-system-instructions")
    if existing == "dangling-symlink":
        assert target.readlink() == tmp_path / "missing"
    else:
        assert target.read_text() == "user instructions"


@pytest.mark.nix
def test_sys_agent_console_tools_and_guidance_use_existing_management_interfaces():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        c = f.nixosConfigurations.airlock-domain-sys-agent.config;
        defaults = import {ROOT}/lifecycle/bootstrap/default-domains/sys-agent.nix;
      in {{
        inherit (defaults) prefs features netvm;
        packages = map (p: p.pname or p.name) c.environment.systemPackages;
        session = c.environment.sessionVariables;
        guiEnabled = c.systemd.services.qubes-gui-agent.enable;
        readyConfig = c.environment.etc."qubes/rpc-config/airlock.AppSessionReady".text;
        instructions = builtins.readFile c.environment.etc."airlock/sys-agent/AGENTS.md".source;
        workspace = {{ inherit (c.systemd.services.airlock-sys-agent-workspace)
          after before requires serviceConfig; }};
        assertionsPass = builtins.all (a: a.assertion) c.assertions;
      }}
    """
    result = subprocess.run(
        [str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
         "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "sys-agent-workspace"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "configuration.json").write_text(result.stdout)
    report = json.loads(result.stdout)
    assert report["assertionsPass"]
    assert report["prefs"]["guivm"] == "sys-gui"
    assert report["features"]["menu-items"] == "*"
    assert report["netvm"] == "sys-firewall"
    assert report["guiEnabled"]
    assert report["readyConfig"] == "force-user='user'\n"
    assert {"codex", "gnome-console", "gnumake", "python3", "ripgrep", "uv", "zellij"} <= set(report["packages"])
    assert report["session"]["CODEX_HOME"] == "/home/user/.codex"
    assert report["session"]["AIRLOCK_LOG_DIR"] == "/home/user/.local/state/airlock/logs"
    assert report["session"]["AIRLOCK_NIX_ROOT"] == "/rw/airlock-builder/merged"
    assert report["session"]["AIRLOCK_NIX_CONFIGURED"] == "1"
    assert report["session"]["UV_PYTHON_DOWNLOADS"] == "never"
    assert report["workspace"]["serviceConfig"]["User"] == "user"
    assert "airlock-qubes-rw-init.service" in report["workspace"]["after"]
    assert "qubes-qrexec-agent.service" in report["workspace"]["before"]
    for command in ("plan", "apply", "status", "verify", "resume", "rollback"):
        assert f"airlock-sys-agent-machine-deploy {command}" in report["instructions"]
