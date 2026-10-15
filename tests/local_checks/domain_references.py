from __future__ import annotations

import json
import os
import shutil
import tarfile
from pathlib import Path
from .support import _git_commit_all, _run, _setup_fake_command_dir, _write_flake


def _write_domain_reference_candidate(
    work_dir: Path, source_role: str
) -> tuple[str, Path]:
    candidate_dir = work_dir / "candidate"
    candidate_dir.mkdir(parents=True)
    _write_flake(
        candidate_dir / "flake.nix",
        """
        {
          outputs = { self }: { };
        }
        """,
    )
    commit = _git_commit_all(
        candidate_dir,
        "fixture candidate",
        name="Airlock Test",
        email="airlock-test@localhost",
    )
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
    (work_dir / "topology.json").write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {
                    "dev": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "green",
                        "netvm": "sys-firewall",
                        "prefs": {},
                        "features": {"airlock-role": "dev"},
                        "services": {},
                        "backend": {"type": "qubes-standalone", "template": None},
                    }
                },
                "prefs": {},
                "qrexec_policy": {"rules": []},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (work_dir / "system.json").write_text(
        json.dumps(
            {
                "schema": "airlock.system.v1",
                "dom0": {
                    "profile": {
                        "source": None,
                        "source_ref": None,
                        "activation_command": "bin/airlock-dom0-profile-activate",
                        "activation_action": "none",
                        "message": "",
                    },
                    "packages": [],
                    "systemd": {"services": {}},
                    "nixos": {
                        name: {
                            "enable": False,
                            "reason": "",
                            "source": None,
                            "source_ref": None,
                            **({"switch_action": "switch"} if name == "system" else {}),
                        }
                        for name in ("system", "kernel", "xen", "bootloader", "qubes")
                    },
                },
                "plan": {
                    "live_actions": [],
                    "reboot_actions": [],
                    "reboot_required": False,
                    "reboot_reasons": [],
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
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
                "source_role": source_role,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    validated_tar = work_dir / "config-candidate-validated.tar"
    with tarfile.open(validated_tar, "w") as tar:
        for member in (
            "config.bundle",
            "validation.json",
            "manifest.json",
            "topology.json",
            "system.json",
        ):
            tar.add(work_dir / member, arcname=member)
    return commit, validated_tar


def _assert_domain_reference_result(
    payload: dict[str, object],
    schema: str,
    expected_status: str,
    expected_reason: str = "",
) -> None:
    assert payload["schema"] == schema
    assert payload["status"] == expected_status
    result = payload["domain_references_result"]
    assert isinstance(result, dict)
    assert result["status"] == expected_status
    if expected_status == "passed":
        references = result["references"]
        assert any(
            reference["kind"] == "netvm"
            and reference["domain"] == "dev"
            and reference["target"] == "sys-firewall"
            for reference in references
        )
        return

    assert payload["reason"] == expected_reason
    missing = result["missing"]
    if expected_reason == "topology references missing domains":
        assert missing == [
            {
                "kind": "netvm",
                "domain": "dev",
                "field": "netvm",
                "target": "sys-firewall",
            }
        ]
    else:
        assert any(
            reference["kind"] == "stock-network-chain"
            and reference["domain"] == "sys-firewall"
            and reference["field"] == "netvm"
            and reference["target"] == "sys-net"
            and reference["reason"] == "stock sys-firewall upstream sys-net is missing"
            for reference in missing
        )


def check_config_archive_domain_references(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    capabilities = work_dir / "xen-capabilities"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    _commit, validated_tar = _write_domain_reference_candidate(work_dir, "dev")
    staged_tar = work_dir / "config-candidate-staged.tar"
    with tarfile.open(staged_tar, "w") as tar:
        tar.add(work_dir / "config.bundle", arcname="config.bundle")
        tar.add(work_dir / "manifest.json", arcname="manifest.json")

    for case_name, sys_firewall_present, sys_net_present, status, reason in (
        (
            "missing-sys-firewall",
            False,
            False,
            "failed",
            "topology references missing domains",
        ),
        (
            "missing-sys-net",
            True,
            False,
            "failed",
            "topology references unusable network domains",
        ),
        ("present", True, True, "passed", ""),
    ):
        case_dir = work_dir / case_name
        bin_dir = case_dir / "bin"
        case_log_dir = case_dir / "logs"
        bin_dir.mkdir(parents=True)
        case_log_dir.mkdir()
        qvm_ls_sys_firewall = (
            "printf 'sys-firewall\\n'" if sys_firewall_present else ":"
        )
        qvm_ls_sys_net = "printf 'sys-net\\n'" if sys_net_present else ":"
        _setup_fake_command_dir(
            bin_dir,
            {
                "qrexec-client": f"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    case "$*" in
                      *"airlock.Config.ValidateCandidate"*)
                        cat >/dev/null
                        cat {validated_tar}
                        ;;
                      *)
                        printf 'unexpected qrexec-client args: %s\\n' "$*" >&2
                        exit 1
                        ;;
                    esac
                """,
                "qvm-check": """
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [ "${1:-}" = "sys-config-validator" ]
                """,
                "qvm-prefs": f"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    case "${{1:-}}:${{2:-}}" in
                      sys-config-validator:netvm)
                        printf '\\n'
                        ;;
                      sys-config-validator:provides_network)
                        printf 'False\\n'
                        ;;
                      sys-firewall:provides_network)
                        {"true" if sys_firewall_present else "false"} || exit 1
                        printf 'True\\n'
                        ;;
                      sys-firewall:netvm)
                        {"true" if sys_firewall_present else "false"} || exit 1
                        printf 'sys-net\\n'
                        ;;
                      sys-net:provides_network)
                        {"true" if sys_net_present else "false"} || exit 1
                        printf 'True\\n'
                        ;;
                      sys-net:netvm)
                        {"true" if sys_net_present else "false"} || exit 1
                        printf '\\n'
                        ;;
                      *)
                        printf 'unexpected qvm-prefs args: %s\\n' "$*" >&2
                        exit 1
                        ;;
                    esac
                """,
                "qvm-start": """
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [ "${1:-}" = "sys-config-validator" ]
                """,
                "qvm-ls": f"""
                    #!/usr/bin/env bash
                    if [ "$*" = "--raw-data --fields NAME --no-spinner" ]; then
                      printf 'dom0\\n'
                      printf 'dev\\n'
                      {qvm_ls_sys_firewall}
                      {qvm_ls_sys_net}
                      exit 0
                    fi
                    printf 'unexpected qvm-ls args: %s\\n' "$*" >&2
                    exit 1
                """,
                "systemctl": """
                    #!/usr/bin/env bash
                    if [ "$1" = "is-active" ]; then
                      exit 0
                    fi
                    exit 1
                """,
            },
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
                "AIRLOCK_LOG_DIR": str(case_log_dir),
            }
        )
        result = _run(
            [
                "bash",
                str(
                    repo_root
                    / "lifecycle/runtime/tools/scripts/archive-config-candidate.sh"
                ),
                "--candidate-tar",
                str(staged_tar),
                "--validate-only",
            ],
            cwd=repo_root,
            env=env,
            stdout=case_dir / "stdout.log",
            stderr=case_dir / "stderr.log",
            check=False,
        )
        if status == "passed":
            assert result.returncode == 0
        else:
            assert result.returncode != 0
        payload = json.loads((case_log_dir / "config-archive.json").read_text())
        _assert_domain_reference_result(
            payload, "airlock.config-archive.v1", status, reason
        )


def check_config_apply_domain_references(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    capabilities = work_dir / "xen-capabilities"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    commit, validated_tar = _write_domain_reference_candidate(
        work_dir, "sys-config-staging"
    )

    for case_name, sys_firewall_present, sys_net_present, status, reason in (
        (
            "missing-sys-firewall",
            False,
            False,
            "failed",
            "topology references missing domains",
        ),
        (
            "missing-sys-net",
            True,
            False,
            "failed",
            "topology references unusable network domains",
        ),
        ("present", True, True, "passed", ""),
    ):
        case_dir = work_dir / case_name
        bin_dir = case_dir / "bin"
        case_log_dir = case_dir / "logs"
        state_dir = case_dir / "state"
        etc_dir = case_dir / "etc"
        setup_capture = case_dir / "setup-topology.json"
        for path in (bin_dir, case_log_dir, state_dir, etc_dir):
            path.mkdir(parents=True)
        qvm_ls_sys_firewall = (
            "printf 'sys-firewall\\n'" if sys_firewall_present else ":"
        )
        qvm_ls_sys_net = "printf 'sys-net\\n'" if sys_net_present else ":"
        _setup_fake_command_dir(
            bin_dir,
            {
                "nix": "#!/usr/bin/env bash\necho unexpected Nix evaluation >&2\nexit 99\n",
                "nix-env": "#!/usr/bin/env bash\necho unexpected profile change >&2\nexit 99\n",
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
                "airlock-setup-config": f"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    topology=""
                    while [ "$#" -gt 0 ]; do
                      case "$1" in
                        --topology)
                          shift
                          topology="$1"
                          ;;
                      esac
                      shift
                    done
                    [ -n "$topology" ] || exit 2
                    cp "$topology" {setup_capture}
                    jq -e '.domains.dev.netvm == "sys-firewall"' "$topology" >/dev/null
                    jq -c -n \\
                      --arg schema "airlock.setup-config.v1" \\
                      '{{schema: $schema, status: "passed", domains_changed: 0, templates_changed: 0}}' \\
                      > "$AIRLOCK_LOG_DIR/setup-config.json"
                    printf 'setup fixture ok\\n'
                """,
                "qrexec-client": f"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    cat >/dev/null
                    jq -c -n \\
                      --arg schema "airlock.config-vault-deployment.v1" \\
                      --arg status "passed" \\
                      --arg candidate_commit "{commit}" \\
                      '{{
                        schema: $schema,
                        status: $status,
                        deployment_status: "passed",
                        candidate_commit: $candidate_commit,
                        vault_commit: "fixture-vault"
                      }}'
                """,
                "qvm-ls": f"""
                    #!/usr/bin/env bash
                    if [ "$*" = "--raw-data --fields NAME --no-spinner" ]; then
                      printf 'dom0\\n'
                      printf 'dev\\n'
                      {qvm_ls_sys_firewall}
                      {qvm_ls_sys_net}
                      exit 0
                    fi
                    printf 'unexpected qvm-ls args: %s\\n' "$*" >&2
                    exit 1
                """,
                "qvm-prefs": f"""
                    #!/usr/bin/env bash
                    set -euo pipefail
                    case "${{1:-}}:${{2:-}}" in
                      sys-firewall:provides_network)
                        {"true" if sys_firewall_present else "false"} || exit 1
                        printf 'True\\n'
                        ;;
                      sys-firewall:netvm)
                        {"true" if sys_firewall_present else "false"} || exit 1
                        printf 'sys-net\\n'
                        ;;
                      sys-net:provides_network)
                        {"true" if sys_net_present else "false"} || exit 1
                        printf 'True\\n'
                        ;;
                      sys-net:netvm)
                        {"true" if sys_net_present else "false"} || exit 1
                        printf '\\n'
                        ;;
                      *)
                        printf 'unexpected qvm-prefs args: %s\\n' "$*" >&2
                        exit 1
                        ;;
                    esac
                """,
                "systemctl": """
                    #!/usr/bin/env bash
                    if [ "$1" = "is-active" ]; then
                      exit 0
                    fi
                    exit 1
                """,
            },
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
                "AIRLOCK_LOG_DIR": str(case_log_dir),
                "AIRLOCK_USE_HOST_NIX_STORE": "1",
                "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
                "AIRLOCK_SYSTEM_ETC_DIR": str(etc_dir),
                # RPC commands are mocked; never wait for the live VM's sockets.
                "AIRLOCK_QREXEC_RUN_DIR": str(case_dir / "no-qrexec-runtime"),
            }
        )
        result = _run(
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
            stdout=case_dir / "stdout.log",
            stderr=case_dir / "stderr.log",
            check=False,
        )
        if status == "passed":
            assert result.returncode == 0
            assert setup_capture.is_file()
        else:
            assert result.returncode != 0
            assert not setup_capture.exists()
        payload = json.loads((case_log_dir / "config-apply.json").read_text())
        _assert_domain_reference_result(
            payload, "airlock.config-apply.v1", status, reason
        )
