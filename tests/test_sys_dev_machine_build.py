from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import signal

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_sys_dev_machine_source_and_build_tools_are_persistent_and_scoped():
    expression = f"""
      let
        flake = builtins.getFlake "{ROOT}";
        guest = flake.nixosConfigurations.airlock-domain-sys-dev.config;
        package = name: builtins.head (builtins.filter (item:
          (item.pname or item.name) == name
        ) guest.environment.systemPackages);
        sourceTool = package "airlock-sys-dev-machine-source";
        buildTool = package "airlock-sys-dev-machine-build";
      in {{
        sourceTool = builtins.readFile "${{sourceTool}}/bin/airlock-sys-dev-machine-source";
        buildTool = builtins.readFile "${{buildTool}}/bin/airlock-sys-dev-machine-build";
        privateStore = guest.airlock.qubeGuest.configNixStoreRoot;
        assertionsPass = builtins.all (entry: entry.assertion) guest.assertions;
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "sys-dev-machine-build"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "report.json").write_text(result.stdout)
    report = json.loads(result.stdout)

    assert report["assertionsPass"] is True
    assert report["privateStore"].startswith("/rw/")
    assert "airlock.sys-dev-machine-source.v1" in report["sourceTool"]
    assert "checkout has local edits; refusing to overwrite it" in report["sourceTool"]
    assert "git init -q" in report["sourceTool"]
    assert 'git -C "$verify_repo" bundle verify' in report["sourceTool"]
    assert "private Nix store needs at least 4 GiB free" in report["buildTool"]
    assert "flake metadata --offline" in report["buildTool"]
    assert 'installable="${installable#.}"' in report["buildTool"]
    assert 'if type == "array" then .[] | .path else keys[] end' in report["buildTool"]
    assert "airlock-pool-send" in report["buildTool"]
    assert '--domain "$target"' in report["buildTool"]
    assert "--domain sys-dev" not in report["buildTool"]
    assert ".activated == false and .retained == true" in report["buildTool"]
    assert "receipt_path" in report["buildTool"]


def test_sys_dev_seed_service_includes_gzip_for_the_embedded_archive():
    guest_module = (ROOT / "lifecycle/misc/modules/sys-dev-guest.nix").read_text()

    assert "pkgs.gnutar pkgs.gzip" in guest_module


@pytest.fixture
def build_script(tmp_path):
    # Render the actual module with host tools, without building a guest system.
    expression = f"""
      let
        tools = builtins.listToAttrs (map (name: {{ inherit name; value = "/tools"; }})
          [ "coreutils" "gitMinimal" "jq" "gawk" "gnugrep" ]);
        module = import {ROOT}/lifecycle/misc/modules/sys-dev-machine-build.nix {{
          config.airlock.qubeGuest = {{ user = "user"; configNixStoreRoot = "/unused"; }};
          lib.escapeShellArg = x: x;
          pkgs = tools // {{ writeShellScriptBin = name: text: text; }};
        }};
      in builtins.elemAt module.environment.systemPackages 1
    """
    rendered = subprocess.run(
        ["nix", "eval", "--raw", "--impure", "--expr", expression],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.replace("/tools/bin/", "")
    script = tmp_path / "build"
    script.write_text(rendered)
    return script


@pytest.mark.parametrize(
    "failure", ["build", "interrupt", "json", "closure", "preflight"]
)
def test_admission_stops_before_sending_when_build_or_metadata_fails(
    tmp_path, build_script, failure
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "flake.lock").write_text("{}")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    nix = binaries / "airlock-config-nix"
    nix.write_text("""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
if [[ "$*" == *"flake metadata"* ]]; then
    [[ "$FAILURE" != preflight ]]
elif [[ "$*" == *"build "* ]]; then
    case "$FAILURE" in
        build) exit 23 ;;
        interrupt) kill -INT "$$"; exit 130 ;;
        json) echo '{invalid'; exit 0 ;;
    esac
    printf '[{"outputs":{"out":"/nix/store/test-system"}}]'
elif [[ "$*" == *"path-info"* ]]; then
    # Even a producer that emits valid-looking partial output must fail closed.
    printf '[{"path":"/nix/store/test-system"}]'
    exit 24
fi
""")
    nix.chmod(0o755)
    sender = binaries / "airlock-pool-send"
    sender.write_text('#!/usr/bin/env bash\necho SENT >> "$CALLS"\n')
    sender.chmod(0o755)
    calls = tmp_path / "calls"
    env = dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        AIRLOCK_MACHINE_REPO=str(repo),
        AIRLOCK_MACHINE_BUILD_STATE=str(tmp_path / "state"),
        AIRLOCK_CONFIG_NIX_STORE_ROOT=str(tmp_path / "store"),
        FAILURE=failure,
        CALLS=str(calls),
    )
    result = subprocess.run(
        ["bash", str(build_script), "admit", "usr-home"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    log = calls.read_text()
    assert "SENT" not in log
    if failure != "closure":
        assert "path-info" not in log
    if failure == "build":
        assert result.returncode == 23
    if failure == "interrupt":
        assert result.returncode in (-signal.SIGINT, 128 + signal.SIGINT)
