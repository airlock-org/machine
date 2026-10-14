"""Installed development tools, Console routing, and persistent agent guidance."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "lifecycle/misc/modules/usr-assistant/workspace-init.sh"
IDENTITY_INIT = ROOT / "lifecycle/misc/modules/usr-assistant/git-identity-init.sh"


@pytest.fixture
def identity_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = home / "airlock-machine"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def initialize_identity(repo, *, check=True):
    return subprocess.run(
        ["bash", str(IDENTITY_INIT), str(repo)], check=check, capture_output=True, text=True,
    )


def git_config(repo, key, *value, scope="--local"):
    return subprocess.check_output(
        ["git", "-C", str(repo), "config", scope, key, *value], text=True,
    ).strip()


def test_git_identity_defaults_allow_commits_and_remain_repository_local(identity_repo):
    initialize_identity(identity_repo)
    assert git_config(identity_repo, "user.name") == "Airlock User"
    assert git_config(identity_repo, "user.email") == "user@airlock.invalid"
    assert not (identity_repo.parent / ".gitconfig").exists()
    subprocess.run([
        "git", "-C", str(identity_repo), "commit", "--allow-empty", "-qm", "First change",
    ], check=True)
    assert subprocess.check_output([
        "git", "-C", str(identity_repo), "log", "-1", "--format=%an <%ae>",
    ], text=True).strip() == "Airlock User <user@airlock.invalid>"


@pytest.mark.parametrize("scope", ["--local", "--global"])
def test_git_identity_preserves_personal_settings_on_repeated_setup(identity_repo, scope):
    git_config(identity_repo, "user.name", "Personal Name", scope=scope)
    git_config(identity_repo, "user.email", "personal@example.com", scope=scope)
    original = (identity_repo / ".git/config").read_bytes()
    initialize_identity(identity_repo)
    initialize_identity(identity_repo)
    assert (identity_repo / ".git/config").read_bytes() == original
    assert git_config(identity_repo, "user.name", scope=scope) == "Personal Name"
    assert git_config(identity_repo, "user.email", scope=scope) == "personal@example.com"


@pytest.mark.parametrize("scope", ["--local", "--global"])
def test_git_identity_fills_only_the_missing_value(identity_repo, scope):
    git_config(identity_repo, "user.name", "Personal Name", scope=scope)
    initialize_identity(identity_repo)
    assert git_config(identity_repo, "user.name", scope=scope) == "Personal Name"
    assert git_config(identity_repo, "user.email") == "user@airlock.invalid"


def test_git_identity_can_initialize_a_checkout_created_after_an_earlier_boot(identity_repo):
    repo = identity_repo.parent / "later-checkout"
    initialize_identity(repo)
    assert not repo.exists()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    initialize_identity(repo)
    assert git_config(repo, "user.name") == "Airlock User"
    assert git_config(repo, "user.email") == "user@airlock.invalid"


def test_git_identity_refuses_to_modify_malformed_configuration(identity_repo):
    config = identity_repo / ".git/config"
    config.write_text("[broken\n")
    assert initialize_identity(identity_repo, check=False).returncode != 0
    assert config.read_text() == "[broken\n"


def initialize(codex_state, instructions, guest_home=None, home_instructions=None):
    guest_home = guest_home or codex_state.parent
    guest_home.mkdir(parents=True, exist_ok=True)
    home_instructions = home_instructions or instructions
    subprocess.run([
        "bash", str(INIT), str(codex_state), str(instructions),
        str(guest_home), str(home_instructions), str(guest_home / "airlock-machine"),
    ], check=True)


def test_workspace_moves_existing_checkout_with_local_work_and_follows_home_guidance(tmp_path):
    guest_home = tmp_path / "home"
    old_repo = guest_home / "dev/airlock-machine"
    old_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(old_repo)], check=True)
    (old_repo / "local-work").write_text("uncommitted work")
    subprocess.run(["git", "-C", str(old_repo), "config", "airlock.test", "retained"], check=True)
    intro = tmp_path / "intro.md"
    intro.write_text("generation one")
    state = guest_home / ".codex"
    initialize(state, tmp_path / "instructions.md", guest_home, intro)
    new_repo = guest_home / "airlock-machine"
    assert not old_repo.exists()
    assert (new_repo / "local-work").read_text() == "uncommitted work"
    assert subprocess.check_output([
        "git", "-C", str(new_repo), "config", "airlock.test",
    ], text=True).strip() == "retained"
    assert (guest_home / "AGENTS.md").is_symlink()
    intro.write_text("generation two")
    initialize(state, tmp_path / "instructions.md", guest_home, intro)
    assert (guest_home / "AGENTS.md").read_text() == "generation two"


@pytest.mark.parametrize("existing", ["file", "symlink", "dangling-symlink"])
def test_workspace_preserves_home_intro_and_destination(tmp_path, existing):
    guest_home = tmp_path / "home"
    old_repo = guest_home / "dev/airlock-machine"
    (old_repo / ".git").mkdir(parents=True)
    (old_repo / "local-work").write_text("old work")
    new_repo = guest_home / "airlock-machine"
    new_repo.mkdir()
    (new_repo / "local-work").write_text("new work")
    intro = guest_home / "AGENTS.md"
    custom = tmp_path / "custom.md"
    custom.write_text("custom intro")
    if existing == "file":
        intro.write_text("custom intro")
    else:
        intro.symlink_to(custom if existing == "symlink" else tmp_path / "missing")
    initialize(guest_home / ".codex", tmp_path / "system.md", guest_home)
    assert (old_repo / "local-work").read_text() == "old work"
    assert (new_repo / "local-work").read_text() == "new work"
    if existing == "dangling-symlink":
        assert intro.readlink() == tmp_path / "missing"
    else:
        assert intro.read_text() == "custom intro"


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


def test_rename_retargets_only_managed_guidance_and_preserves_workspace(tmp_path):
    home = tmp_path / "home"
    state = home / ".codex"
    state.mkdir(parents=True)
    (home / "AGENTS.md").symlink_to("/etc/airlock/sys-agent/home-AGENTS.md")
    (state / "AGENTS.md").symlink_to("/etc/airlock/sys-agent/AGENTS.md")
    (state / "auth.json").write_text("existing credentials")
    (home / "notes.txt").write_text("existing work")
    instructions = tmp_path / "new-generation.md"
    intro = tmp_path / "new-intro.md"
    initialize(state, instructions, home, intro)
    assert (home / "AGENTS.md").readlink() == intro
    assert (state / "AGENTS.md").readlink() == instructions
    assert (state / "auth.json").read_text() == "existing credentials"
    assert (home / "notes.txt").read_text() == "existing work"
    # Re-running initialization must keep deliberate overrides.
    (home / "AGENTS.md").unlink()
    (home / "AGENTS.md").write_text("user guidance")
    (state / "AGENTS.md").unlink()
    custom = tmp_path / "custom.md"
    (state / "AGENTS.md").symlink_to(custom)
    initialize(state, instructions, home, intro)
    assert (home / "AGENTS.md").read_text() == "user guidance"
    assert (state / "AGENTS.md").readlink() == custom


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
def test_usr_assistant_console_tools_and_guidance_use_existing_management_interfaces():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        c = f.nixosConfigurations.airlock-domain-usr-assistant.config;
        defaults = import {ROOT}/lifecycle/bootstrap/default-domains/usr-assistant.nix;
      in {{
        inherit (defaults) prefs features netvm;
        packages = map (p: p.pname or p.name) c.environment.systemPackages;
        session = c.environment.sessionVariables;
        guiEnabled = c.systemd.services.qubes-gui-agent.enable;
        readyConfig = c.environment.etc."qubes/rpc-config/airlock.AppSessionReady".text;
        instructions = builtins.readFile c.environment.etc."airlock/usr-assistant/AGENTS.md".source;
        intro = builtins.readFile c.environment.etc."airlock/usr-assistant/home-AGENTS.md".source;
        repoPath = c.airlock.qubeGuest.configRepoPath;
        workspace = {{ inherit (c.systemd.services.airlock-usr-assistant-workspace)
          after before requires serviceConfig; }};
        identity = {{ inherit (c.systemd.services.airlock-usr-assistant-git-identity)
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
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "usr-assistant-workspace"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "configuration.json").write_text(result.stdout)
    report = json.loads(result.stdout)
    assert report["assertionsPass"]
    assert report["prefs"]["guivm"] == "sys-gui"
    assert report["prefs"]["virt_mode"] == "pvh"
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
    assert "airlock-machine-source-seed.service" in report["workspace"]["before"]
    assert report["identity"]["serviceConfig"]["User"] == "user"
    assert report["identity"]["serviceConfig"]["Type"] == "oneshot"
    assert "airlock-usr-assistant-workspace.service" in report["identity"]["requires"]
    assert {"airlock-usr-assistant-workspace.service", "airlock-machine-source-seed.service"} <= set(report["identity"]["after"])
    assert "qubes-qrexec-agent.service" in report["identity"]["before"]
    assert report["repoPath"] == "/home/user/airlock-machine"
    assert "~/airlock-machine" in report["intro"]
    assert len(report["intro"].splitlines()) <= 12
    for command in ("plan", "apply", "status", "verify", "resume", "rollback"):
        assert f"airlock-usr-assistant-machine-deploy {command}" in report["instructions"]
