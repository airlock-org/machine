from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from .support import _git_commit_all, _physical_store_path, _run, _setup_fake_command_dir, _write_executable, _write_flake


def check_airlock_cli(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    repo = work_dir / "repo"
    captured_tar = work_dir / "captured-candidate.tar"
    qrexec_log = work_dir / "qrexec.log"
    status_json = work_dir / "status.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    (repo / "modules").mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    status_json.write_text(
        json.dumps(
            {
                "schema": "airlock.config-status.v1",
                "status": "applied",
                "deployment": {
                    "current": {
                        "schema": "airlock.config-deployment.v1",
                        "candidate_commit": "base000000000000000000000000000000000000",
                    }
                },
                "host_generation": {
                    "active": True,
                    "current_source": "/nix/store/base-system",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_executable(
        bin_dir / "qrexec-client-vm",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        target="$1"
        service="$2"
        printf '%s %s\n' "$target" "$service" >> "$QREXEC_LOG"
        case "$service" in
          airlock.Config.Status)
            cat "$STATUS_JSON"
            ;;
          airlock.Config.Check)
            cat > "$CAPTURED_TAR"
            jq -c -n --arg schema "airlock.config-archive.v1" --arg status "passed" \
              '{schema: $schema, status: $status}'
            ;;
          airlock.Config.Apply)
            cat > "$CAPTURED_TAR"
            jq -c -n --arg schema "airlock.config-apply.v1" --arg status "passed" \
              '{schema: $schema, status: $status}'
            ;;
          airlock.Config.Rollback)
            jq -c -n --arg schema "airlock.config-rollback.v1" --arg status "passed" \
              '{schema: $schema, status: $status}'
            ;;
          *)
            printf 'unexpected service: %s\n' "$service" >&2
            exit 2
            ;;
        esac
        """,
    )
    _write_flake(
        repo / "flake.nix",
        """
        {
          description = "Airlock CLI test repo";
          outputs = { self }: {
            airlock.topology = {
              schema = "airlock.qubes-topology.v1";
              templates = {};
              kernels = {};
              domains = {};
              prefs = {};
              qrexec_policy.rules = [];
            };
          };
        }
        """,
    )
    (repo / "modules/controller.nix").write_text(
        "initial controller\n", encoding="utf-8"
    )
    _git_commit_all(
        repo,
        "initial candidate",
        name="Airlock CLI Test",
        email="airlock-cli-test@localhost",
    )
    (repo / "modules/controller.nix").write_text(
        "self-hosted controller change\n", encoding="utf-8"
    )
    _run(["git", "add", "modules/controller.nix"], cwd=repo)
    _run(
        [
            "git",
            "-c",
            "user.name=Airlock CLI Test",
            "-c",
            "user.email=airlock-cli-test@localhost",
            "commit",
            "-m",
            "change controller source",
        ],
        cwd=repo,
    )
    expected_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    expected_tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    airlock_out = _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.airlock-cli",
            "--no-link",
            "--print-out-paths",
        ]
    ).stdout.strip()
    airlock_bin = Path(_physical_store_path(repo_root, airlock_out)) / "bin/airlock"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AIRLOCK_QREXEC_CLIENT": str(bin_dir / "qrexec-client-vm"),
            "QREXEC_LOG": str(qrexec_log),
            "STATUS_JSON": str(status_json),
            "CAPTURED_TAR": str(captured_tar),
        }
    )

    for subcommand, output_path, schema in (
        ("status", work_dir / "status-output.json", "airlock.config-status.v1"),
        ("check", work_dir / "check.json", "airlock.config-archive.v1"),
        ("apply", work_dir / "apply.json", "airlock.config-apply.v1"),
        ("rollback", work_dir / "rollback.json", "airlock.config-rollback.v1"),
    ):
        _run(
            [str(airlock_bin), subcommand],
            cwd=repo,
            env=env,
            stdout=output_path,
        )
        output = json.loads(output_path.read_text(encoding="utf-8"))
        assert output["schema"] == schema
        expected_status = "applied" if subcommand == "status" else "passed"
        assert output["status"] == expected_status

        if subcommand == "check":
            with tarfile.open(captured_tar) as tar:
                members = sorted(tar.getnames())
                tar.extractall(work_dir / "candidate")
            assert "config.bundle" in members
            assert "manifest.json" in members
            manifest = json.loads(
                (work_dir / "candidate/manifest.json").read_text(encoding="utf-8")
            )
            assert manifest["schema"] == "airlock.config-candidate.v1"
            assert manifest["commit"] == expected_commit
            assert manifest["tree"] == expected_tree
            assert manifest["branch"] == "main"
            assert manifest["source_role"] == "cmd"
            assert manifest["transport"] == "qrexec"
            assert manifest["base_generation"]["schema"] == "airlock.config-status.v1"
            assert (
                manifest["base_generation"]["deployment"]["current"]["candidate_commit"]
                == "base000000000000000000000000000000000000"
            )
            assert manifest["requested_targets"] == ["airlock"]
            _run(
                [
                    "git",
                    "clone",
                    str(work_dir / "candidate/config.bundle"),
                    str(work_dir / "cloned"),
                ]
            )
            assert (work_dir / "cloned/modules/controller.nix").read_text(
                encoding="utf-8"
            ).strip() == "self-hosted controller change"

    with (repo / "modules/controller.nix").open("a", encoding="utf-8") as handle:
        handle.write("dirty\n")
    result = _run(
        [str(airlock_bin), "check"],
        cwd=repo,
        env=env,
        stdout=work_dir / "dirty.json",
        stderr=work_dir / "dirty.stderr",
        check=False,
    )
    assert result.returncode != 0
    assert "worktree is dirty" in (work_dir / "dirty.stderr").read_text(
        encoding="utf-8"
    )
    qrexec_lines = qrexec_log.read_text(encoding="utf-8").splitlines()
    for line in (
        "dom0 airlock.Config.Status",
        "dom0 airlock.Config.Check",
        "dom0 airlock.Config.Apply",
        "dom0 airlock.Config.Rollback",
    ):
        assert line in qrexec_lines


def check_config_control(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    control_log_dir = work_dir / "control-logs"
    state_dir = work_dir / "state"
    host_profile = work_dir / "host-system-profile"
    host_current = work_dir / "run-current-system"
    payload_marker = work_dir / "payload-executed"
    activation_marker = work_dir / "activated"
    fake_call_log = work_dir / "fake-calls.log"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (bin_dir, control_log_dir, state_dir):
        path.mkdir(parents=True)

    _write_executable(
        bin_dir / "airlock-config-status",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        jq -c -n --arg schema "airlock.config-status.v1" --arg status "applied" \
          '{schema: $schema, status: $status, deployment: {current: {candidate_commit: "base"}}}'
        """,
    )
    _write_executable(
        bin_dir / "airlock-config-archive-candidate",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        candidate_tar=""
        validate_only=0
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --candidate-tar) shift; candidate_tar="$1" ;;
            --validate-only) validate_only=1 ;;
          esac
          shift
        done
        [ -n "$candidate_tar" ] || exit 2
        [ "$validate_only" = 1 ] || exit 3
        printf 'archive %s validate-only=%s\n' "$candidate_tar" "$validate_only" >> "$FAKE_CALL_LOG"
        tmpdir="$(mktemp -d)"
        trap 'rm -rf "$tmpdir"' EXIT
        tar -C "$tmpdir" -xf "$candidate_tar"
        commit="$(jq -r '.commit' "$tmpdir/manifest.json")"
        scenario="$(jq -r '.scenario // "valid"' "$tmpdir/manifest.json")"
        if [ "$scenario" = "invalid-eval" ]; then
          jq -c -n --arg schema "airlock.config-archive.v1" --arg status "failed" --arg commit "$commit" \
            '{schema: $schema, status: $status, reason: "candidate validation failed", candidate_commit: $commit, validation: {schema: "airlock.config-validation.v1", status: "failed", commit: $commit}}' \
            > "$AIRLOCK_LOG_DIR/config-archive.json"
          exit 1
        fi
        jq -c -n --arg schema "airlock.config-archive.v1" --arg status "passed" --arg commit "$commit" \
          '{schema: $schema, status: $status, reason: "validated by fake validator", candidate_commit: $commit, validation_commit: $commit, validation: {schema: "airlock.config-validation.v1", status: "passed", commit: $commit}}' \
          > "$AIRLOCK_LOG_DIR/config-archive.json"
        printf 'fake archive ok\n'
        """,
    )
    _write_executable(
        bin_dir / "airlock-config-apply",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        candidate_tar=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --candidate-tar) shift; candidate_tar="$1" ;;
          esac
          shift
        done
        [ -n "$candidate_tar" ] || exit 2
        printf 'apply %s\n' "$candidate_tar" >> "$FAKE_CALL_LOG"
        tmpdir="$(mktemp -d)"
        trap 'rm -rf "$tmpdir"' EXIT
        tar -C "$tmpdir" -xf "$candidate_tar"
        commit="$(jq -r '.commit' "$tmpdir/manifest.json")"
        scenario="$(jq -r '.scenario // "valid"' "$tmpdir/manifest.json")"
        case "$scenario" in
          valid)
            printf '%s\n' "$commit" > "$ACTIVATION_MARKER"
            jq -c -n --arg schema "airlock.config-apply.v1" --arg status "passed" --arg commit "$commit" \
              '{schema: $schema, status: $status, candidate_commit: $commit, system_apply_result: {status: "passed"}}' \
              > "$AIRLOCK_LOG_DIR/config-apply.json"
            ;;
          build-failed)
            jq -c -n --arg schema "airlock.config-apply.v1" --arg status "failed" --arg commit "$commit" \
              '{schema: $schema, status: $status, reason: "system build failed", candidate_commit: $commit}' \
              > "$AIRLOCK_LOG_DIR/config-apply.json"
            exit 1
            ;;
          test-failed)
            jq -c -n --arg schema "airlock.config-apply.v1" --arg status "failed" --arg commit "$commit" \
              '{schema: $schema, status: $status, reason: "candidate tests failed", candidate_commit: $commit}' \
              > "$AIRLOCK_LOG_DIR/config-apply.json"
            exit 1
            ;;
          invalid-eval)
            jq -c -n --arg schema "airlock.config-apply.v1" --arg status "failed" --arg commit "$commit" \
              '{schema: $schema, status: $status, reason: "candidate validation failed", candidate_commit: $commit}' \
              > "$AIRLOCK_LOG_DIR/config-apply.json"
            exit 1
            ;;
        esac
        printf 'fake apply ok\n'
        """,
    )
    _write_executable(
        bin_dir / "nix-env",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'nix-env %s\n' "$*" >> "$FAKE_CALL_LOG"
        exit 1
        """,
    )

    def make_candidate(scenario: str, output: Path) -> str:
        tmpdir = work_dir / f"candidate-{scenario}"
        repo = tmpdir / "repo"
        repo.mkdir(parents=True)
        _write_flake(
            repo / "flake.nix",
            """
            {
              outputs = { self }: {
                airlock.topology = {
                  schema = "airlock.qubes-topology.v1";
                  templates = {};
                  kernels = {};
                  domains = {};
                  prefs = {};
                  qrexec_policy.rules = [];
                };
              };
            }
            """,
        )
        commit = _git_commit_all(
            repo,
            f"{scenario} candidate",
            name="Airlock Control Test",
            email="airlock-control-test@localhost",
        )
        _run(
            [
                "git",
                "bundle",
                "create",
                str(tmpdir / "config.bundle"),
                "HEAD",
                "refs/heads/main",
            ],
            cwd=repo,
        )
        (tmpdir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "airlock.config-candidate.v1",
                    "commit": commit,
                    "branch": "main",
                    "source_role": "cmd",
                    "scenario": scenario,
                    "payload": f"touch {payload_marker}",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with tarfile.open(output, "w") as tar:
            tar.add(tmpdir / "config.bundle", arcname="config.bundle")
            tar.add(tmpdir / "manifest.json", arcname="manifest.json")
        return commit

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AIRLOCK_CONFIG_CONTROL_LOG_DIR": str(control_log_dir),
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_HOST_SYSTEM_PROFILE": str(host_profile),
            "AIRLOCK_HOST_CURRENT_SYSTEM_LINK": str(host_current),
            "FAKE_CALL_LOG": str(fake_call_log),
            "ACTIVATION_MARKER": str(activation_marker),
        }
    )
    script = (
        repo_root / "lifecycle/runtime/tools/scripts/config-control.sh"
    )
    _run(
        ["bash", str(script), "status"],
        cwd=repo_root,
        env=env,
        stdout=work_dir / "status.json",
    )
    status = json.loads((work_dir / "status.json").read_text(encoding="utf-8"))
    assert status["schema"] == "airlock.config-status.v1"
    assert status["status"] == "applied"

    valid_tar = work_dir / "valid.tar"
    make_candidate("valid", valid_tar)
    for mode, output_path in (
        ("check", work_dir / "check-valid.json"),
        ("apply", work_dir / "apply-valid.json"),
    ):
        activation_marker.unlink(missing_ok=True)
        with valid_tar.open("rb") as stdin:
            result = subprocess.run(
                ["bash", str(script), mode],
                cwd=repo_root,
                env=env,
                check=False,
                stdin=stdin,
                stdout=subprocess.PIPE,
            )
        output_path.write_bytes(result.stdout)
        assert result.returncode == 0
        payload = json.loads(result.stdout.decode("utf-8"))
        assert payload["status"] == "passed"
        if mode == "check":
            assert payload["schema"] == "airlock.config-archive.v1"
            assert "validate-only=1" in fake_call_log.read_text(encoding="utf-8")
            assert not payload_marker.exists()
        else:
            assert payload["schema"] == "airlock.config-apply.v1"
            assert activation_marker.is_file()
            assert not payload_marker.exists()

    for scenario in ("invalid-eval", "build-failed", "test-failed"):
        activation_marker.unlink(missing_ok=True)
        tar_path = work_dir / f"{scenario}.tar"
        make_candidate(scenario, tar_path)
        with tar_path.open("rb") as stdin:
            result = subprocess.run(
                ["bash", str(script), "apply"],
                cwd=repo_root,
                env=env,
                check=False,
                stdin=stdin,
                stdout=subprocess.PIPE,
            )
        (work_dir / f"apply-{scenario}.json").write_bytes(result.stdout)
        assert result.returncode != 0
        failed = json.loads(result.stdout.decode("utf-8"))
        assert failed["schema"] == "airlock.config-apply.v1"
        assert failed["status"] == "failed"
        assert not activation_marker.exists()

    previous_system = work_dir / "previous-system"
    current_system = work_dir / "current-system-source"
    (previous_system / "bin").mkdir(parents=True)
    current_system.mkdir()
    _write_executable(
        previous_system / "bin/switch-to-configuration",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$1" > "$AIRLOCK_SYSTEM_STATE_DIR/rollback-switch-action"
        ln -sfn "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" "$AIRLOCK_HOST_CURRENT_SYSTEM_LINK"
        """,
    )
    host_profile.symlink_to(current_system)
    (state_dir / "previous-host-system").write_text(
        str(previous_system) + "\n", encoding="utf-8"
    )
    _run(
        ["bash", str(script), "rollback"],
        cwd=repo_root,
        env=env,
        stdout=work_dir / "rollback.json",
    )
    rollback = json.loads((work_dir / "rollback.json").read_text(encoding="utf-8"))
    assert rollback["schema"] == "airlock.config-rollback.v1"
    assert rollback["status"] == "passed"
    assert rollback["method"] == "state"
    assert rollback["target_system"] == str(previous_system)
    assert os.readlink(host_profile) == str(previous_system)
    assert os.readlink(host_current) == str(previous_system)
    assert (state_dir / "rollback-switch-action").read_text(
        encoding="utf-8"
    ).strip() == "switch"

    with tarfile.open(work_dir / "bad.tar", "w") as tar:
        tar.add(valid_tar, arcname="valid.tar")
    with (work_dir / "bad.tar").open("rb") as stdin:
        result = subprocess.run(
            ["bash", str(script), "check"],
            cwd=repo_root,
            env=env,
            check=False,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    (work_dir / "bad.json").write_bytes(result.stdout)
    (work_dir / "bad.stderr").write_bytes(result.stderr)
    assert result.returncode != 0
    bad = json.loads(result.stdout.decode("utf-8"))
    assert bad["schema"] == "airlock.config-control.v1"
    assert bad["status"] == "failed"


def check_config_status(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    status_log_dir = work_dir / "logs"
    state_dir = work_dir / "state"
    policy_dir = work_dir / "policy.d"
    topology_json = work_dir / "topology.json"
    current_topology_json = state_dir / "current-topology.json"
    profile_dir = work_dir / "profiles"
    store_dir = work_dir / "store"
    system_profile = profile_dir / "airlock-system"
    host_system_profile = profile_dir / "system"
    run_current_system = work_dir / "run-current-system"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        status_log_dir,
        state_dir,
        policy_dir,
        profile_dir,
        store_dir,
    ):
        path.mkdir(parents=True)

    topology = {
        "schema": "airlock.qubes-topology.v1",
        "templates": {},
        "kernels": {},
        "domains": {
            "sys-config-vault": {
                "class": "StandaloneVM",
                "template": None,
                "backend": {"type": "nix-closure"},
            },
            "work-fedora": {"class": "AppVM", "template": "fedora-44-xfce"},
        },
        "prefs": {},
        "qrexec_policy": {
            "rules": [
                {
                    "service": "airlock.PolicyProbe",
                    "argument": "*",
                    "source": "dom0",
                    "target": "sys-config-vault",
                    "action": "allow",
                }
            ]
        },
    }
    current_topology = json.loads(json.dumps(topology))
    current_topology["domains"]["work-fedora-from-current"] = {
        "class": "AppVM",
        "template": "fedora-44-xfce",
    }
    topology_json.write_text(
        json.dumps(topology, sort_keys=True) + "\n", encoding="utf-8"
    )
    current_topology_json.write_text(
        json.dumps(current_topology, sort_keys=True) + "\n", encoding="utf-8"
    )
    (status_log_dir / "setup-config.json").write_text(
        json.dumps(
            {
                "schema": "airlock.setup-config.v1",
                "status": "passed",
                "domains_changed": 2,
                "restart_required": True,
                "restart_actions": [
                    {
                        "type": "domain-pref",
                        "action": "restart",
                        "domain": "sys-config-vault",
                        "field": "memory",
                        "value": "1024",
                        "reason": "domain is running; restart required for this change to fully take effect",
                        "state": "Running",
                    }
                ],
                "live_actions": [
                    {
                        "type": "domain-pref",
                        "action": "set",
                        "domain": "sys-config-vault",
                        "field": "memory",
                        "value": "1024",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (status_log_dir / "config-apply.json").write_text(
        '{"schema":"airlock.config-apply.v1","status":"passed","candidate_commit":"abc123"}\n',
        encoding="utf-8",
    )
    (state_dir / "current-system.json").write_text(
        '{"schema":"airlock.system.v1","dom0":{"profile":{"message":"from-test"}}}\n',
        encoding="utf-8",
    )
    (state_dir / "current-plan.json").write_text(
        json.dumps(
            {
                "live_actions": [],
                "reboot_actions": [],
                "reboot_required": False,
                "reboot_reasons": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "last-apply.json").write_text(
        json.dumps(
            {
                "schema": "airlock.system-apply.v1",
                "status": "passed",
                "host_system_status": "passed",
                "host_system_source": str(store_dir / "host-system"),
                "host_system_switch_action": "switch",
                "reboot_required": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for filename, payload in (
        (
            "current-deployment.json",
            {
                "schema": "airlock.config-deployment.v1",
                "status": "passed",
                "deployment_id": "deployment-current",
            },
        ),
        (
            "last-deployment.json",
            {
                "schema": "airlock.config-deployment.v1",
                "status": "failed",
                "deployment_id": "deployment-last-attempt",
            },
        ),
        (
            "current-config-apply.json",
            {
                "schema": "airlock.config-apply.v1",
                "status": "passed",
                "deployment_id": "deployment-current",
            },
        ),
        (
            "last-config-apply.json",
            {
                "schema": "airlock.config-apply.v1",
                "status": "failed",
                "deployment_id": "deployment-last-attempt",
            },
        ),
    ):
        (state_dir / filename).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    for name in ("airlock-profile", "host-system", "old-host-system"):
        (store_dir / name).mkdir()
    system_profile.symlink_to(store_dir / "airlock-profile")
    (profile_dir / "system-2-link").symlink_to(store_dir / "host-system")
    host_system_profile.symlink_to("system-2-link")
    run_current_system.symlink_to(store_dir / "host-system")
    (policy_dir / "90-airlock.policy").write_text(
        "\n".join(
            (
                "# Managed by Airlock setup-config.",
                "airlock.PolicyProbe * dom0 sys-config-vault allow",
                "airlock.PolicyProbe * @anyvm @anyvm deny",
                "",
            )
        ),
        encoding="utf-8",
    )
    _setup_fake_command_dir(
        bin_dir,
        {
            "qvm-ls": """
                #!/usr/bin/env bash
                set -euo pipefail
                case "$*" in
                  "--raw-data --fields NAME,CLASS,STATE,TEMPLATE,LABEL --no-spinner")
                    cat <<'QVM'
                sys-config-vault|StandaloneVM|Running||black
                work-fedora|AppVM|Halted|fedora-44-xfce|green
                other|AppVM|Halted|fedora-43-xfce|red
                QVM
                    ;;
                  *)
                    printf 'unexpected qvm-ls args: %s\\n' "$*" >&2
                    exit 2
                    ;;
                esac
            """,
        },
    )
    out = _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.airlock-config-status",
            "--no-link",
            "--print-out-paths",
        ],
        cwd=repo_root,
    ).stdout.strip()
    wrapper = Path(_physical_store_path(repo_root, out)) / "bin/airlock-config-status"

    def run_status(output: Path, *, boot_topology: bool) -> dict[str, object]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AIRLOCK_QVM_LS_BIN": str(bin_dir / "qvm-ls"),
                "AIRLOCK_LOG_DIR": str(status_log_dir),
                "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
                "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
                "AIRLOCK_CONFIG_STATUS_SYSTEM_PROFILE": str(system_profile),
                "AIRLOCK_CONFIG_STATUS_HOST_SYSTEM_PROFILE": str(host_system_profile),
                "AIRLOCK_CONFIG_STATUS_RUN_CURRENT_SYSTEM": str(run_current_system),
            }
        )
        if boot_topology:
            env["AIRLOCK_CONFIG_STATUS_BOOT_TOPOLOGY_JSON"] = str(topology_json)
        else:
            env["AIRLOCK_CONFIG_STATUS_TOPOLOGY_JSON"] = str(topology_json)
        _run([str(wrapper)], cwd=repo_root, env=env, stdout=output)
        return json.loads(output.read_text())

    result = run_status(work_dir / "status.json", boot_topology=False)
    assert result["schema"] == "airlock.config-status.v1"
    assert result["status"] == "applied"
    assert result["topology"]["present"] is True
    assert "sys-config-vault" in result["topology"]["domain_names"]
    assert result["topology"]["template_names"] == []
    assert result["topology"]["qrexec_policy_rule_count"] == 1
    assert result["qvm"]["available"] is True
    assert result["qvm"]["status"] == "ok"
    assert len(result["qvm"]["domains"]) == 3
    assert len(result["qvm"]["managed_domains"]) == 2
    assert result["qrexec_policy"]["exists"] is True
    assert result["qrexec_policy"]["rule_count"] == 2
    assert result["pending_reboot"]["required"] is False
    assert result["pending_reboot"]["actions"] == []
    assert result["pending_reboot"]["reasons"] == []
    host_system_target = str(store_dir / "host-system")
    profile_target = str(store_dir / "airlock-profile")
    assert result["host_generation"]["active"] is True
    assert result["host_generation"]["current_source"] == host_system_target
    assert result["host_generation"]["staged_source"] == ""
    assert result["host_generation"]["profile"]["resolved_target"] == host_system_target
    assert result["host_generation"]["running"]["resolved_target"] == host_system_target
    assert result["host_generation"]["last_apply"]["source"] == host_system_target
    assert result["host_generation"]["last_apply"]["switch_action"] == "switch"
    assert result["host_generation"]["pending_reboot"]["required"] is False
    pending_vm_restarts = result["pending_vm_restarts"]
    assert pending_vm_restarts["required"] is True
    assert len(pending_vm_restarts["actions"]) == 1
    assert pending_vm_restarts["actions"][0]["domain"] == "sys-config-vault"
    assert pending_vm_restarts["domains"] == ["sys-config-vault"]
    assert pending_vm_restarts["reasons"] == [
        "domain is running; restart required for this change to fully take effect"
    ]
    assert result["setup_source"] == "log"
    assert result["setup"]["status"] == "passed"
    assert len(result["setup"]["restart_actions"]) == 1
    assert result["apply"]["status"] == "passed"
    assert result["deployment"]["current"]["deployment_id"] == "deployment-current"
    assert (
        result["deployment"]["last_attempt"]["deployment_id"]
        == "deployment-last-attempt"
    )
    assert result["deployment"]["current_apply"]["status"] == "passed"
    assert result["deployment"]["last_apply_result"]["status"] == "failed"
    system = result["system"]
    assert system["current_system"]["dom0"]["profile"]["message"] == "from-test"
    assert system["current_plan"]["reboot_actions"] == []
    assert system["current_plan"]["reboot_required"] is False
    assert system["pending_reboot"]["required"] is False
    assert system["host_generation"]["active"] is True
    assert system["host_generation"]["current_source"] == host_system_target
    assert system["host_generation"]["last_apply"]["source"] == host_system_target
    assert system["last_apply"]["host_system_status"] == "passed"
    assert system["profile_target"] == profile_target
    assert system["profile_resolved_target"] == profile_target
    assert system["host_system_profile_target"] == "system-2-link"
    assert system["host_system_profile_resolved_target"] == host_system_target
    assert system["run_current_system_target"] == host_system_target
    assert system["run_current_system_resolved_target"] == host_system_target

    current_result = run_status(work_dir / "status-current.json", boot_topology=True)
    assert current_result["schema"] == "airlock.config-status.v1"
    assert current_result["topology"]["source"] == "current"
    assert (
        current_result["paths"]["topology"]
        == current_result["paths"]["current_topology"]
    )
    assert current_result["topology"]["boot_present"] is True
    assert current_result["topology"]["current_present"] is True
    assert "work-fedora-from-current" in current_result["topology"]["domain_names"]
    assert len(current_result["qvm"]["managed_domains"]) == 2

    (state_dir / "current-plan.json").write_text(
        json.dumps(
            {
                "live_actions": [],
                "reboot_actions": [
                    {
                        "type": "dom0-nixos-system",
                        "action": "boot",
                        "reason": "install next dom0 boot generation",
                    }
                ],
                "reboot_required": True,
                "reboot_reasons": ["install next dom0 boot generation"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    run_current_system.unlink()
    run_current_system.symlink_to(store_dir / "old-host-system")
    reboot_result = run_status(work_dir / "status-reboot.json", boot_topology=True)
    assert reboot_result["schema"] == "airlock.config-status.v1"
    assert reboot_result["pending_reboot"]["required"] is True
    assert len(reboot_result["pending_reboot"]["actions"]) == 1
    assert reboot_result["pending_reboot"]["actions"][0]["type"] == (
        "dom0-nixos-system"
    )
    assert reboot_result["pending_reboot"]["reasons"] == [
        "install next dom0 boot generation"
    ]
    assert reboot_result["host_generation"]["active"] is False
    assert reboot_result["host_generation"]["current_source"] == str(
        store_dir / "old-host-system"
    )
    assert reboot_result["host_generation"]["staged_source"] == host_system_target
    assert reboot_result["host_generation"]["pending_reboot"]["required"] is True
    assert reboot_result["system"]["pending_reboot"]["required"] is True
    assert len(reboot_result["system"]["pending_reboot"]["actions"]) == 1
    assert reboot_result["system"]["host_generation"]["active"] is False

    run_current_system.unlink()
    run_current_system.symlink_to(store_dir / "host-system")
    consumed_result = run_status(
        work_dir / "status-reboot-consumed.json", boot_topology=True
    )
    assert consumed_result["schema"] == "airlock.config-status.v1"
    assert consumed_result["pending_reboot"]["required"] is False
    assert consumed_result["pending_reboot"]["actions"] == []
    assert consumed_result["pending_reboot"]["reasons"] == []
    assert consumed_result["host_generation"]["active"] is True
    assert consumed_result["host_generation"]["current_source"] == host_system_target
    assert consumed_result["host_generation"]["staged_source"] == ""
    assert consumed_result["host_generation"]["pending_reboot"]["required"] is False
    assert consumed_result["pending_vm_restarts"]["required"] is True
    assert consumed_result["system"]["pending_reboot"]["required"] is False
    assert consumed_result["system"]["pending_reboot"]["actions"] == []
    assert consumed_result["system"]["pending_reboot"]["reasons"] == []
    assert consumed_result["system"]["host_generation"]["active"] is True
    assert consumed_result["system"]["current_plan"]["reboot_required"] is True
    assert len(consumed_result["system"]["current_plan"]["reboot_actions"]) == 1

    (status_log_dir / "setup-config.json").rename(
        status_log_dir / "setup-config.json.saved"
    )
    (state_dir / "current-setup.json").write_text(
        json.dumps(
            {
                "schema": "airlock.setup-config.v1",
                "status": "passed",
                "domains_changed": 0,
                "restart_required": False,
                "restart_actions": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    current_setup_result = run_status(
        work_dir / "status-current-setup.json", boot_topology=True
    )
    assert current_setup_result["schema"] == "airlock.config-status.v1"
    assert current_setup_result["setup_source"] == "current"
    assert current_setup_result["paths"]["current_setup"] != ""
    assert current_setup_result["setup"]["status"] == "passed"
    assert current_setup_result["setup_log"] is None
    assert current_setup_result["current_setup"]["status"] == "passed"
    assert current_setup_result["pending_vm_restarts"]["required"] is False
    assert current_setup_result["pending_vm_restarts"]["actions"] == []
    assert current_setup_result["pending_vm_restarts"]["domains"] == []
    assert current_setup_result["pending_vm_restarts"]["reasons"] == []
