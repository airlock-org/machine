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
        storeTool = package "airlock-config-nix-store";
        deployTool = package "airlock-sys-dev-machine-deploy";
      in {{
        sourceTool = builtins.readFile "${{sourceTool}}/bin/airlock-sys-dev-machine-source";
        buildTool = builtins.readFile "${{buildTool}}/bin/airlock-sys-dev-machine-build";
        storeTool = builtins.readFile "${{storeTool}}/bin/airlock-config-nix-store";
        deployTool = builtins.readFile "${{deployTool}}/bin/airlock-sys-dev-machine-deploy";
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
    assert "local-overlay-store" in report["storeTool"]
    assert "nix-store" in report["storeTool"]
    assert "sys-dev-machine-deploy.py" in report["deployTool"]


def test_shared_machine_seed_service_includes_gzip_for_the_embedded_archive():
    seed_module = (ROOT / "lifecycle/misc/modules/machine-source-seed.nix").read_text()
    sys_dev_module = (ROOT / "lifecycle/misc/modules/sys-dev-guest.nix").read_text()
    domains = (ROOT / "lifecycle/bootstrap/default-domains/default.nix").read_text()

    assert "pkgs.gnutar pkgs.gzip" in seed_module
    assert "./machine-source-seed.nix" in sys_dev_module
    assert "../../misc/modules/machine-source-seed.nix" in domains


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
                "schema": "airlock.sys-dev-offline-inputs.v1",
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
