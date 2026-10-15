from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import signal

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def build_script(tmp_path):
    # Render the actual module with host tools, without building a guest system.
    expression = f"""
      let
        tools = builtins.listToAttrs (map (name: {{ inherit name; value = "/tools"; }})
          [ "coreutils" "gitMinimal" "jq" "gawk" "gnugrep" ]);
        module = import {ROOT}/lifecycle/misc/modules/usr-assistant-machine-build.nix {{
          config.airlock.qubeGuest = {{ user = "user"; configNixStoreRoot = "/unused";
            configRepoPath = "/home/user/airlock-machine"; }};
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
@pytest.mark.nix
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


@pytest.mark.nix
def test_offline_build_uses_pinned_sources_and_rejects_changed_lock(
    tmp_path, build_script
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
    source = "/nix/store/" + "a" * 32 + "-source"
    store = tmp_path / "store"
    (store / source.lstrip("/")).mkdir(parents=True)
    manifest = tmp_path / "flake-inputs.json"
    import hashlib

    lock_hash = hashlib.sha256((repo / "flake.lock").read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "schema": "airlock.sys-agent-offline-inputs.v1",
                "lock_hash": lock_hash,
                "inputs": {"nixpkgs": source},
            }
        )
    )
    script = tmp_path / "offline-build"
    script.write_text(
        build_script.read_text().replace(
            "/run/airlock-builder/pool/.airlock-builder/flake-inputs.json",
            str(manifest),
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_nix = binaries / "airlock-config-nix"
    fake_nix.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CALLS"\ncase "$*" in *\' build \'*) echo \'[{"outputs":{"out":"/nix/store/test-system"}}]\' ;; esac\n'
    )
    fake_nix.chmod(0o755)
    calls = tmp_path / "calls"
    env = dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        AIRLOCK_MACHINE_REPO=str(repo),
        AIRLOCK_CONFIG_NIX_STORE_ROOT=str(store),
        CALLS=str(calls),
    )
    result = subprocess.run(
        ["bash", str(script), "build", "--offline"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/nix/store/test-system"
    assert (
        "--offline --no-write-lock-file --override-input nixpkgs path:" + source
        in calls.read_text()
    )
    manifest.write_text(manifest.read_text().replace(lock_hash, "b" * 64))
    calls.unlink()
    result = subprocess.run(
        ["bash", str(script), "build", "--offline"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "differs from the image offline inputs" in result.stderr
    assert not calls.exists()
