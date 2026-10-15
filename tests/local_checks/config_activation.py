from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from .support import _git_commit_all, _run, _setup_fake_command_dir, _write_executable, _write_flake


def check_config_system_apply_boot(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    candidate_dir = work_dir / "candidate"
    apply_log_dir = work_dir / "logs"
    capabilities = work_dir / "xen-capabilities"
    host_system_source = candidate_dir / "dom0-system"
    state_dir = work_dir / "state"
    etc_dir = work_dir / "etc"
    system_profile = work_dir / "airlock-system-profile"
    host_system_profile = work_dir / "host-system-profile"
    host_current_system_link = work_dir / "current-system"
    validated_tar = work_dir / "config-candidate-validated.tar"
    deployment_tar_capture = work_dir / "deployment.tar"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        candidate_dir / "airlock",
        apply_log_dir,
        host_system_source / "bin",
        etc_dir,
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    shutil.copy2(
        repo_root / "lifecycle/misc/lib/system.nix",
        candidate_dir / "airlock/system-schema.nix",
    )
    _write_executable(
        host_system_source / "bin/switch-to-configuration",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$1" > "$AIRLOCK_SYSTEM_STATE_DIR/host-switch-action"
        printf '%s\n' "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" > "$AIRLOCK_SYSTEM_STATE_DIR/host-switch-source"
        if [ "$1" != "boot" ]; then
          ln -sfn "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" "$AIRLOCK_HOST_CURRENT_SYSTEM_LINK"
        fi
        """,
    )
    _write_flake(
        candidate_dir / "flake.nix",
        """
        {
          outputs =
            { self }:
            let
              systemSchema = import ./airlock/system-schema.nix;
            in
            {
              airlock.topology = {
                schema = "airlock.qubes-topology.v1";
                templates = { };
                kernels = { };
                domains = { };
                prefs = { };
                qrexec_policy.rules = [ ];
              };

              airlock.system = systemSchema.mkSystem {
                dom0.nixos.system = {
                  source = "dom0-system";
                  switchAction = "boot";
                  reason = "install next dom0 boot generation";
                };
              };
            };
        }
        """,
    )
    commit = _git_commit_all(
        candidate_dir,
        "fixture boot candidate",
        name="Airlock Test",
        email="airlock-test@localhost",
    )
    expected_host_system_source = state_dir / f"sources/{commit}/dom0.nixos.system"
    _run(
        [
            "git",
            "bundle",
            "create",
            str(work_dir / "config.bundle"),
            "HEAD",
            "refs/heads/main",
        ],
        cwd=candidate_dir,
    )
    _run(
        ["nix", "eval", "--json", f"{candidate_dir}#airlock.topology"],
        stdout=work_dir / "topology.json",
    )
    _run(
        ["nix", "eval", "--json", f"{candidate_dir}#airlock.system"],
        stdout=work_dir / "system.json",
    )
    (work_dir / "validation.json").write_text(
        json.dumps(
            {
                "schema": "airlock.config-validation.v1",
                "status": "passed",
                "commit": commit,
                "checks": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (work_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "airlock.config-candidate.v1",
                "commit": commit,
                "branch": "main",
                "source_role": "sys-config-staging",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with tarfile.open(validated_tar, "w") as tar:
        for member in (
            "config.bundle",
            "validation.json",
            "manifest.json",
            "topology.json",
            "system.json",
        ):
            tar.add(work_dir / member, arcname=member)

    _setup_fake_command_dir(
        bin_dir,
        {
            "airlock-config-archive-candidate": f"""
                #!/usr/bin/env bash
                set -euo pipefail
                cp {validated_tar} "$AIRLOCK_LOG_DIR/config-candidate-validated.tar"
                jq -c -n \\
                  --arg schema "airlock.config-archive.v1" \\
                  --arg status "passed" \\
                  --arg candidate_commit "{commit}" \\
                  '{{
                    schema: $schema,
                    status: $status,
                    candidate_commit: $candidate_commit,
                    validation_commit: $candidate_commit,
                    validation: {{
                      schema: "airlock.config-validation.v1",
                      status: "passed",
                      commit: $candidate_commit
                    }},
                    vault_archive: {{
                      schema: "airlock.config-vault-archive.v1",
                      status: "passed",
                      candidate_commit: $candidate_commit
                    }}
                  }}' > "$AIRLOCK_LOG_DIR/config-archive.json"
            """,
            "airlock-setup-config": """
                #!/usr/bin/env bash
                set -euo pipefail
                jq -c -n \
                  --arg schema "airlock.setup-config.v1" \
                  --arg status "passed" \
                  '{schema: $schema, status: $status, domains_changed: 0, templates_changed: 0}' \
                  > "$AIRLOCK_LOG_DIR/setup-config.json"
            """,
            "qrexec-client": """
                #!/usr/bin/env bash
                set -euo pipefail
                cat > "$DEPLOYMENT_TAR_CAPTURE_FIXTURE"
                jq -c -n \
                  --arg schema "airlock.config-vault-deployment.v1" \
                  --arg status "passed" \
                  --arg candidate_commit "$CANDIDATE_COMMIT_FIXTURE" \
                  '{
                    schema: $schema,
                    status: $status,
                    deployment_status: "passed",
                    candidate_commit: $candidate_commit,
                    vault_commit: "fixture-vault"
                  }'
            """,
            "systemctl": """
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1" in
                is-active)
                  exit 0
                  ;;
                esac
                printf 'unexpected systemctl args:' >&2
                printf ' [%s]' "$@" >&2
                printf '\n' >&2
                exit 1
            """,
        },
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CANDIDATE_COMMIT_FIXTURE": commit,
            "DEPLOYMENT_TAR_CAPTURE_FIXTURE": str(deployment_tar_capture),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(apply_log_dir),
            "AIRLOCK_USE_HOST_NIX_STORE": "1",
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_SYSTEM_ETC_DIR": str(etc_dir),
            "AIRLOCK_SYSTEM_PROFILE": str(system_profile),
            "AIRLOCK_HOST_SYSTEM_PROFILE": str(host_system_profile),
            "AIRLOCK_HOST_CURRENT_SYSTEM_LINK": str(host_current_system_link),
        }
    )
    _run(
        [
            "bash",
            str(
                repo_root
                / "lifecycle/runtime/tools/scripts/apply-config-candidate.sh"
            ),
            "--apply",
        ],
        cwd=repo_root,
        env=env,
        stdout=work_dir / "stdout.log",
        stderr=work_dir / "stderr.log",
    )

    apply_payload = json.loads((apply_log_dir / "config-apply.json").read_text())
    assert apply_payload["schema"] == "airlock.config-apply.v1"
    assert apply_payload["status"] == "passed"
    assert apply_payload["candidate_commit"] == commit
    system_apply = apply_payload["system_apply_result"]
    assert system_apply["status"] == "passed"
    assert system_apply["profile_status"] == "skipped"
    assert system_apply["host_system_status"] == "passed"
    assert system_apply["host_system_source"] == str(expected_host_system_source)
    assert system_apply["host_system_switch_action"] == "boot"
    assert system_apply["host_current_system"] == ""
    assert system_apply["reboot_required"] is True
    assert len(system_apply["reboot_actions"]) == 1
    assert system_apply["reboot_actions"][0]["type"] == "dom0-nixos-system"
    assert system_apply["reboot_reasons"] == ["install next dom0 boot generation"]
    assert apply_payload["deployment"]["status"] == "passed"
    assert apply_payload["deployment"]["system_apply"]["status"] == "passed"
    assert apply_payload["deployment"]["system_apply"]["reboot_required"] is True
    assert len(apply_payload["deployment"]["system_apply"]["reboot_actions"]) == 1

    last_system = json.loads((state_dir / "last-system.json").read_text())
    assert last_system["schema"] == "airlock.system.v1"
    assert last_system["dom0"]["nixos"]["system"]["switch_action"] == "boot"
    assert last_system["plan"]["reboot_required"] is True
    assert len(last_system["plan"]["reboot_actions"]) == 1
    assert last_system["plan"]["reboot_actions"][0]["type"] == "dom0-nixos-system"
    current_deployment = json.loads((state_dir / "current-deployment.json").read_text())
    assert current_deployment["schema"] == "airlock.config-deployment.v1"
    assert current_deployment["status"] == "passed"
    assert current_deployment["system_apply"]["reboot_required"] is True
    assert len(current_deployment["system_apply"]["reboot_actions"]) == 1
    assert (state_dir / "current-host-system-switch-action").read_text().strip() == (
        "boot"
    )
    assert (state_dir / "host-switch-action").read_text().strip() == "boot"
    assert (state_dir / "current-host-system").read_text().strip() == str(
        expected_host_system_source
    )
    assert (state_dir / "host-switch-source").read_text().strip() == str(
        expected_host_system_source
    )
    assert os.readlink(host_system_profile) == str(expected_host_system_source)
    assert not host_current_system_link.exists()


def check_config_system_apply(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    candidate_dir = work_dir / "candidate"
    apply_log_dir = work_dir / "logs"
    capabilities = work_dir / "xen-capabilities"
    profile_source = candidate_dir / "dom0-profile"
    host_system_source = candidate_dir / "dom0-system"
    state_dir = work_dir / "state"
    etc_dir = work_dir / "etc"
    system_profile = work_dir / "airlock-system-profile"
    host_system_profile = work_dir / "host-system-profile"
    host_current_system_link = work_dir / "current-system"
    validated_tar = work_dir / "config-candidate-validated.tar"
    deployment_tar_capture = work_dir / "deployment.tar"
    read_only_topology_target = work_dir / "read-only-boot-topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        candidate_dir,
        apply_log_dir,
        etc_dir,
        profile_source / "bin",
        profile_source / "share/airlock",
        host_system_source / "bin",
        host_system_source / "etc",
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    read_only_topology_target.write_text(
        '{"schema":"airlock.qubes-topology.v1","domains":{}}\n', encoding="utf-8"
    )
    read_only_topology_target.chmod(0o444)
    (etc_dir / "qubes-topology.json").symlink_to(read_only_topology_target)
    (profile_source / "share/airlock/profile.json").write_text(
        '{"schema":"airlock.dom0-profile.v1","message":"profile-from-test","built_by":"config-system-apply-test"}\n',
        encoding="utf-8",
    )
    _write_executable(
        profile_source / "bin/airlock-dom0-profile-activate",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        install -d -m 0755 "$AIRLOCK_SYSTEM_STATE_DIR" "$AIRLOCK_SYSTEM_ETC_DIR"
        cp "$(dirname "$0")/../share/airlock/profile.json" "$AIRLOCK_SYSTEM_STATE_DIR/current-profile.json"
        cp "$AIRLOCK_SYSTEM_JSON" "$AIRLOCK_SYSTEM_STATE_DIR/current-system.json"
        cp "$AIRLOCK_SYSTEM_PLAN_JSON" "$AIRLOCK_SYSTEM_STATE_DIR/current-plan.json"
        cp "$(dirname "$0")/../share/airlock/profile.json" "$AIRLOCK_SYSTEM_ETC_DIR/system-profile.json"
        printf 'activated\n' > "$AIRLOCK_SYSTEM_STATE_DIR/profile-activated"
        """,
    )
    (host_system_source / "etc/inside-config-message").write_text(
        "profile-from-test\n", encoding="utf-8"
    )
    _write_executable(
        host_system_source / "bin/switch-to-configuration",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "${AIRLOCK_FAIL_HOST_SWITCH:-0}" = "1" ]; then
          printf 'fixture host switch failed\n' >&2
          exit 42
        fi
        printf '%s\n' "$1" > "$AIRLOCK_SYSTEM_STATE_DIR/host-switch-action"
        printf '%s\n' "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" > "$AIRLOCK_SYSTEM_STATE_DIR/host-switch-source"
        ln -sfn "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" "$AIRLOCK_HOST_CURRENT_SYSTEM_LINK"
        cp "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/etc/inside-config-message" "$AIRLOCK_SYSTEM_ETC_DIR/inside-config-applied-message"
        """,
    )
    _write_flake(
        candidate_dir / "flake.nix",
        """
        {
          outputs =
            { self }:
            {
              airlock.topology = {
                schema = "airlock.qubes-topology.v1";
                templates = { };
                kernels = { };
                domains = { };
                prefs = { };
                qrexec_policy.rules = [ ];
              };

              airlock.system = {
                schema = "airlock.system.v1";
                dom0 = {
                  profile = {
                    source = "dom0-profile";
                    source_ref = null;
                    activation_command = "bin/airlock-dom0-profile-activate";
                    activation_action = "run";
                    message = "profile-from-test";
                  };
                  packages = [
                    { name = "airlock-dom0-profile"; }
                  ];
                  systemd.services = {
                    "airlock-test-restart.service" = {
                      action = "restart";
                      reason = "restart fixture service";
                    };
                    "airlock-test-reload.service" = {
                      action = "reload";
                      reason = "reload fixture service";
                    };
                  };
                  nixos = {
                    system = {
                      enable = true;
                      reason = "dom0 system change requires reboot";
                      source = "dom0-system";
                      source_ref = null;
                      switch_action = "switch";
                    };
                    kernel = {
                      enable = false;
                      reason = "dom0 kernel change requires reboot";
                      source = null;
                      source_ref = null;
                    };
                    xen = {
                      enable = false;
                      reason = "dom0 xen change requires reboot";
                      source = null;
                      source_ref = null;
                    };
                    bootloader = {
                      enable = false;
                      reason = "dom0 bootloader change requires reboot";
                      source = null;
                      source_ref = null;
                    };
                    qubes = {
                      enable = false;
                      reason = "dom0 qubes change requires reboot";
                      source = null;
                      source_ref = null;
                    };
                  };
                };
                plan = {
                  live_actions = [
                    {
                      type = "dom0-profile";
                      action = "run";
                      source = "dom0-profile";
                      source_ref = null;
                      activation_command = "bin/airlock-dom0-profile-activate";
                      message = "profile-from-test";
                    }
                    {
                      type = "dom0-nixos-system";
                      action = "switch";
                      source = "dom0-system";
                      source_ref = null;
                      reason = "dom0 system change requires reboot";
                    }
                    {
                      type = "systemd-service";
                      name = "airlock-test-restart.service";
                      action = "restart";
                      reason = "restart fixture service";
                    }
                    {
                      type = "systemd-service";
                      name = "airlock-test-reload.service";
                      action = "reload";
                      reason = "reload fixture service";
                    }
                  ];
                  reboot_actions = [ ];
                  reboot_required = false;
                  reboot_reasons = [ ];
                };
              };
            };
        }
        """,
    )
    commit = _git_commit_all(
        candidate_dir,
        "fixture candidate",
        name="Airlock Test",
        email="airlock-test@localhost",
    )
    expected_profile_source = state_dir / f"sources/{commit}/dom0.profile"
    expected_host_system_source = state_dir / f"sources/{commit}/dom0.nixos.system"
    _run(
        [
            "git",
            "bundle",
            "create",
            str(work_dir / "config.bundle"),
            "HEAD",
            "refs/heads/main",
        ],
        cwd=candidate_dir,
    )
    _run(
        ["nix", "eval", "--json", f"{candidate_dir}#airlock.topology"],
        stdout=work_dir / "topology.json",
    )
    _run(
        ["nix", "eval", "--json", f"{candidate_dir}#airlock.system"],
        stdout=work_dir / "system.json",
    )
    for filename, payload in (
        (
            "validation.json",
            {
                "schema": "airlock.config-validation.v1",
                "status": "passed",
                "commit": commit,
                "checks": [],
            },
        ),
        (
            "manifest.json",
            {
                "schema": "airlock.config-candidate.v1",
                "commit": commit,
                "branch": "main",
                "source_role": "sys-config-staging",
            },
        ),
    ):
        (work_dir / filename).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    with tarfile.open(validated_tar, "w") as tar:
        for member in (
            "config.bundle",
            "validation.json",
            "manifest.json",
            "topology.json",
            "system.json",
        ):
            tar.add(work_dir / member, arcname=member)
    _setup_fake_command_dir(
        bin_dir,
        {
            "airlock-config-archive-candidate": f"""
                #!/usr/bin/env bash
                set -euo pipefail
                cp {validated_tar} "$AIRLOCK_LOG_DIR/config-candidate-validated.tar"
                jq -c -n \\
                  --arg schema "airlock.config-archive.v1" \\
                  --arg status "passed" \\
                  --arg candidate_commit "{commit}" \\
                  '{{
                    schema: $schema,
                    status: $status,
                    candidate_commit: $candidate_commit,
                    validation_commit: $candidate_commit,
                    validation: {{
                      schema: "airlock.config-validation.v1",
                      status: "passed",
                      commit: $candidate_commit
                    }},
                    vault_archive: {{
                      schema: "airlock.config-vault-archive.v1",
                      status: "passed",
                      candidate_commit: $candidate_commit
                    }}
                  }}' > "$AIRLOCK_LOG_DIR/config-archive.json"
                printf 'archive fixture ok\\n'
            """,
            "airlock-setup-config": """
                #!/usr/bin/env bash
                set -euo pipefail
                jq -c -n \
                  --arg schema "airlock.setup-config.v1" \
                  --arg status "passed" \
                  '{schema: $schema, status: $status, domains_changed: 0, templates_changed: 0}' \
                  > "$AIRLOCK_LOG_DIR/setup-config.json"
                printf 'setup fixture ok\n'
            """,
            "qrexec-client": """
                #!/usr/bin/env bash
                set -euo pipefail
                cat > "$DEPLOYMENT_TAR_CAPTURE_FIXTURE"
                jq -c -n \
                  --arg schema "airlock.config-vault-deployment.v1" \
                  --arg status "passed" \
                  --arg candidate_commit "$CANDIDATE_COMMIT_FIXTURE" \
                  '{
                    schema: $schema,
                    status: $status,
                    deployment_status: "passed",
                    candidate_commit: $candidate_commit,
                    vault_commit: "fixture-vault"
                  }'
            """,
            "systemctl": """
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "$AIRLOCK_SYSTEM_STATE_DIR"
                case "$1" in
                is-active)
                  exit 0
                  ;;
                restart|reload)
                  printf '%s %s\n' "$1" "$2" >> "$AIRLOCK_SYSTEM_STATE_DIR/systemctl-calls"
                  printf '%s %s ok\n' "$1" "$2"
                  exit 0
                  ;;
                esac
                printf 'unexpected systemctl args:' >&2
                printf ' [%s]' "$@" >&2
                printf '\n' >&2
                exit 1
            """,
        },
    )

    def run_apply(
        *,
        output_dir: Path,
        state: Path,
        etc: Path,
        system_link: Path,
        host_link: Path,
        current_link: Path,
        deployment_capture: Path,
        fail_host_switch: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "CANDIDATE_COMMIT_FIXTURE": commit,
                "DEPLOYMENT_TAR_CAPTURE_FIXTURE": str(deployment_capture),
                "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
                "AIRLOCK_LOG_DIR": str(output_dir),
                "AIRLOCK_USE_HOST_NIX_STORE": "1",
                "AIRLOCK_SYSTEM_STATE_DIR": str(state),
                "AIRLOCK_SYSTEM_ETC_DIR": str(etc),
                "AIRLOCK_SYSTEM_PROFILE": str(system_link),
                "AIRLOCK_HOST_SYSTEM_PROFILE": str(host_link),
                "AIRLOCK_HOST_CURRENT_SYSTEM_LINK": str(current_link),
            }
        )
        if fail_host_switch:
            env["AIRLOCK_FAIL_HOST_SWITCH"] = "1"
        return _run(
            [
                "bash",
                str(
                    repo_root
                    / "lifecycle/runtime/tools/scripts/apply-config-candidate.sh"
                ),
                "--apply",
            ],
            cwd=repo_root,
            env=env,
            stdout=work_dir / ("fail-stdout.log" if fail_host_switch else "stdout.log"),
            stderr=work_dir / ("fail-stderr.log" if fail_host_switch else "stderr.log"),
            check=False,
        )

    result = run_apply(
        output_dir=apply_log_dir,
        state=state_dir,
        etc=etc_dir,
        system_link=system_profile,
        host_link=host_system_profile,
        current_link=host_current_system_link,
        deployment_capture=deployment_tar_capture,
    )
    assert result.returncode == 0
    apply_payload = json.loads((apply_log_dir / "config-apply.json").read_text())
    assert apply_payload["schema"] == "airlock.config-apply.v1"
    assert apply_payload["status"] == "passed"
    assert apply_payload["candidate_commit"] == commit
    system_apply = apply_payload["system_apply_result"]
    assert system_apply["status"] == "passed"
    assert system_apply["profile_status"] == "passed"
    assert system_apply["host_system_status"] == "passed"
    assert system_apply["systemd_status"] == "passed"
    assert system_apply["host_system_source"] == str(expected_host_system_source)
    assert system_apply["host_system_switch_action"] == "switch"
    assert system_apply["reboot_actions"] == []
    assert system_apply["reboot_required"] is False
    assert apply_payload["systemd_actions_result"]["status"] == "passed"
    assert apply_payload["deployment"]["status"] == "passed"
    assert apply_payload["deployment"]["system_apply"]["status"] == "passed"
    assert apply_payload["deployment"]["systemd_actions"]["status"] == "passed"
    for filename in (
        "current-deployment.json",
        "last-deployment.json",
        "current-config-apply.json",
        "last-config-apply.json",
    ):
        payload = json.loads((state_dir / filename).read_text())
        assert payload["status"] == "passed"
        assert payload["candidate_commit"] == commit
    current_profile = json.loads((state_dir / "current-profile.json").read_text())
    assert current_profile["schema"] == "airlock.dom0-profile.v1"
    assert current_profile["message"] == "profile-from-test"
    current_system = json.loads((state_dir / "current-system.json").read_text())
    assert current_system["schema"] == "airlock.system.v1"
    assert current_system["dom0"]["profile"]["message"] == "profile-from-test"
    assert current_system["dom0"]["nixos"]["system"]["enable"] is True
    assert current_system["dom0"]["nixos"]["system"]["source"] == str(
        expected_host_system_source
    )
    assert current_system["dom0"]["nixos"]["system"]["switch_action"] == "switch"
    assert (
        current_system["dom0"]["systemd"]["services"]["airlock-test-restart.service"][
            "action"
        ]
        == "restart"
    )
    assert (
        current_system["dom0"]["systemd"]["services"]["airlock-test-reload.service"][
            "action"
        ]
        == "reload"
    )
    assert current_system["plan"]["reboot_actions"] == []
    assert current_system["plan"]["reboot_required"] is False
    current_topology = json.loads((state_dir / "current-topology.json").read_text())
    assert current_topology["schema"] == "airlock.qubes-topology.v1"
    assert len(current_topology["domains"]) == 0
    current_setup = json.loads((state_dir / "current-setup.json").read_text())
    assert current_setup["schema"] == "airlock.setup-config.v1"
    assert current_setup["status"] == "passed"
    assert os.readlink(etc_dir / "qubes-topology.json") == str(
        read_only_topology_target
    )
    assert (state_dir / "profile-activated").read_text().strip() == "activated"
    systemctl_calls = (state_dir / "systemctl-calls").read_text().splitlines()
    assert "restart airlock-test-restart.service" in systemctl_calls
    assert "reload airlock-test-reload.service" in systemctl_calls
    assert os.readlink(system_profile) == str(expected_profile_source)
    assert (expected_profile_source / "bin/airlock-dom0-profile-activate").is_file()
    assert os.readlink(host_system_profile) == str(expected_host_system_source)
    assert os.readlink(host_current_system_link) == str(expected_host_system_source)
    assert (expected_host_system_source / "bin/switch-to-configuration").is_file()
    assert (state_dir / "host-switch-action").read_text().strip() == "switch"
    assert (state_dir / "host-switch-source").read_text().strip() == str(
        expected_host_system_source
    )
    assert (
        etc_dir / "inside-config-applied-message"
    ).read_text().strip() == "profile-from-test"
    with tarfile.open(deployment_tar_capture) as tar:
        members = set(tar.getnames())
        for member in (
            "system.json",
            "resolved-system.json",
            "system-plan.json",
            "system-apply.json",
            "system-builds.json",
            "systemd-actions.json",
            "logs/profile-activation.log",
            "logs/systemd-actions.log",
        ):
            assert member in members
        deployment = json.load(tar.extractfile("deployment.json"))
        systemd_actions = json.load(tar.extractfile("systemd-actions.json"))
    assert systemd_actions["schema"] == "airlock.systemd-actions.v1"
    assert systemd_actions["status"] == "passed"
    assert any(
        action["name"] == "airlock-test-restart.service"
        and action["action"] == "restart"
        and action["status"] == "passed"
        for action in systemd_actions["actions"]
    )
    assert any(
        action["name"] == "airlock-test-reload.service"
        and action["action"] == "reload"
        and action["status"] == "passed"
        for action in systemd_actions["actions"]
    )
    assert deployment["artifacts"]["systemd_actions"] == "systemd-actions.json"
    assert deployment["systemd_actions"]["status"] == "passed"

    fail_log_dir = work_dir / "fail-logs"
    fail_state_dir = work_dir / "fail-state"
    fail_etc_dir = work_dir / "fail-etc"
    fail_deployment_tar = work_dir / "deployment-failed.tar"
    fail_log_dir.mkdir()
    failed = run_apply(
        output_dir=fail_log_dir,
        state=fail_state_dir,
        etc=fail_etc_dir,
        system_link=work_dir / "fail-airlock-system-profile",
        host_link=work_dir / "fail-host-system-profile",
        current_link=work_dir / "fail-current-system",
        deployment_capture=fail_deployment_tar,
        fail_host_switch=True,
    )
    assert failed.returncode != 0
    failed_apply = json.loads((fail_log_dir / "config-apply.json").read_text())
    assert failed_apply["schema"] == "airlock.config-apply.v1"
    assert failed_apply["status"] == "failed"
    assert failed_apply["reason"] == "system apply failed"
    assert failed_apply["system_apply_result"]["status"] == "failed"
    assert failed_apply["system_apply_result"]["profile_status"] == "passed"
    assert failed_apply["system_apply_result"]["host_system_status"] == "skipped"
    assert failed_apply["system_apply_result"]["reason"] == (
        "dom0 NixOS switch-to-configuration switch failed"
    )
    assert failed_apply["deployment"]["status"] == "failed"
    assert failed_apply["deployment"]["system_apply"]["status"] == "failed"
    assert failed_apply["deployment_archive"]["status"] == "passed"
    fail_topology = json.loads((fail_state_dir / "current-topology.json").read_text())
    assert fail_topology["schema"] == "airlock.qubes-topology.v1"
    assert len(fail_topology["domains"]) == 0
    last_deployment = json.loads((fail_state_dir / "last-deployment.json").read_text())
    assert last_deployment["schema"] == "airlock.config-deployment.v1"
    assert last_deployment["status"] == "failed"
    assert last_deployment["candidate_commit"] == commit
    last_apply = json.loads((fail_state_dir / "last-config-apply.json").read_text())
    assert last_apply["schema"] == "airlock.config-apply.v1"
    assert last_apply["status"] == "failed"
    assert last_apply["candidate_commit"] == commit
    assert not (fail_state_dir / "current-deployment.json").exists()
    assert not (fail_state_dir / "current-config-apply.json").exists()
    with tarfile.open(fail_deployment_tar) as tar:
        fail_deployment = json.load(tar.extractfile("deployment.json"))
        fail_system_apply = json.load(tar.extractfile("system-apply.json"))
        fail_systemd_actions = json.load(tar.extractfile("systemd-actions.json"))
    assert fail_deployment["schema"] == "airlock.config-deployment.v1"
    assert fail_deployment["status"] == "failed"
    assert fail_deployment["reason"] == "system apply failed"
    assert fail_deployment["system_apply"]["status"] == "failed"
    assert fail_system_apply["schema"] == "airlock.system-apply.v1"
    assert fail_system_apply["status"] == "failed"
    assert fail_system_apply["reason"] == (
        "dom0 NixOS switch-to-configuration switch failed"
    )
    assert fail_systemd_actions["schema"] == "airlock.systemd-actions.v1"
    assert fail_systemd_actions["status"] == "skipped"


def check_config_boot_verify(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    result_log_dir = work_dir / "logs"
    status_json = work_dir / "status.json"
    failed_status_json = work_dir / "status-failed.json"
    result_json = result_log_dir / "config-boot-verify.json"

    shutil.rmtree(work_dir, ignore_errors=True)
    bin_dir.mkdir(parents=True)
    result_log_dir.mkdir()
    _write_executable(
        bin_dir / "airlock-config-status",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        cat "$AIRLOCK_FAKE_CONFIG_STATUS"
        """,
    )

    status_json.write_text(
        json.dumps(
            {
                "schema": "airlock.config-status.v1",
                "pending_reboot": {"required": False, "actions": [], "reasons": []},
                "system": {
                    "last_apply": {"host_system_source": "/nix/store/new-system"},
                    "host_system_profile_resolved_target": "/nix/store/new-system",
                    "run_current_system_resolved_target": "/nix/store/new-system",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AIRLOCK_LOG_DIR": str(result_log_dir),
            "AIRLOCK_FAKE_CONFIG_STATUS": str(status_json),
            "AIRLOCK_EXPECTED_HOST_SYSTEM_SOURCE": "/nix/store/new-system",
        }
    )
    _run(
        [
            "bash",
            str(
                repo_root
                / "lifecycle/runtime/tools/scripts/config-boot-verify.sh"
            ),
        ],
        cwd=repo_root,
        env=env,
        stdout=work_dir / "stdout.log",
    )
    passed = json.loads(result_json.read_text(encoding="utf-8"))
    assert passed["schema"] == "airlock.config-boot-verify.v1"
    assert passed["status"] == "passed"
    assert passed["expected_host_system_source"] == "/nix/store/new-system"
    assert passed["last_apply_host_system_source"] == "/nix/store/new-system"
    assert passed["host_system_profile_resolved_target"] == "/nix/store/new-system"
    assert passed["run_current_system_resolved_target"] == "/nix/store/new-system"
    assert passed["pending_reboot"]["required"] is False
    assert passed["status_json"]["schema"] == "airlock.config-status.v1"

    failed_status_json.write_text(
        json.dumps(
            {
                "schema": "airlock.config-status.v1",
                "pending_reboot": {
                    "required": True,
                    "actions": [
                        {
                            "type": "dom0-nixos-system",
                            "switch_action": "boot",
                            "reason": "install next dom0 boot generation",
                        }
                    ],
                    "reasons": ["install next dom0 boot generation"],
                },
                "system": {
                    "last_apply": {"host_system_source": "/nix/store/new-system"},
                    "host_system_profile_resolved_target": "/nix/store/new-system",
                    "run_current_system_resolved_target": "/nix/store/old-system",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    env["AIRLOCK_FAKE_CONFIG_STATUS"] = str(failed_status_json)
    result = _run(
        [
            "bash",
            str(
                repo_root
                / "lifecycle/runtime/tools/scripts/config-boot-verify.sh"
            ),
        ],
        cwd=repo_root,
        env=env,
        stdout=work_dir / "failed-stdout.log",
        stderr=work_dir / "failed-stderr.log",
        check=False,
    )
    assert result.returncode != 0
    failed = json.loads(result_json.read_text(encoding="utf-8"))
    assert failed["schema"] == "airlock.config-boot-verify.v1"
    assert failed["status"] == "failed"
    assert failed["reason"] == "running system does not point at expected generation"
    assert failed["pending_reboot"]["required"] is True
