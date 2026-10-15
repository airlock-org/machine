from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from .support import _run, _setup_fake_command_dir


def check_setup_config_qrexec_policy(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    policy_file = policy_dir / "90-airlock.policy"
    state_dir = work_dir / "state"
    capabilities = work_dir / "xen-capabilities"
    mutation_log = work_dir / "mutations.log"
    systemctl_log = work_dir / "systemctl.log"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (setup_log_dir, policy_dir, state_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    mutation_log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {},
                "prefs": {},
                "qrexec_policy": {
                    "rules": [
                        {
                            "service": "airlock.PolicyProbe",
                            "argument": "*",
                            "source": "dom0",
                            "target": "sys-config-vault",
                            "action": "allow",
                        },
                        {
                            "service": "airlock.PolicyProbe",
                            "argument": "*",
                            "source": "@anyvm",
                            "target": "@anyvm",
                            "action": "deny",
                        },
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    unexpected = """
    #!/usr/bin/env bash
    printf 'unexpected command execution: %s %s\n' "$0" "$*" >&2
    exit 97
    """
    commands = {
        name: unexpected
        for name in (
            "qvm-create",
            "qvm-features",
            "qvm-prefs",
            "qvm-service",
            "qvm-template-postprocess",
            "qvm-volume",
            "qubes-prefs",
            "qubesd-query",
        )
    }
    commands["qvm-ls"] = "#!/usr/bin/env bash\nprintf 'dom0\n'\n"
    commands["systemctl"] = """
    #!/usr/bin/env bash
    case "$1" in
      is-active)
        exit 0
        ;;
      reload)
        printf 'systemctl %s\n' "$*" >> "$SYSTEMCTL_LOG"
        exit 1
        ;;
      try-restart)
        printf 'systemctl %s\n' "$*" >> "$SYSTEMCTL_LOG"
        exit 0
        ;;
    esac
    exit 1
    """
    _setup_fake_command_dir(bin_dir, commands)
    for command_name in ("cmp", "cp", "grep", "jq", "mkdir", "chmod", "rm"):
        path = shutil.which(command_name)
        if path is not None and not (bin_dir / command_name).exists():
            (bin_dir / command_name).symlink_to(path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MUTATION_LOG": str(mutation_log),
            "SYSTEMCTL_LOG": str(systemctl_log),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_LOG_DIR": str(setup_log_dir),
        }
    )
    command = [
        "bash",
        str(repo_root / "lifecycle/misc/tools/scripts/setup-config.sh"),
        "--apply",
        "--topology",
        str(topology),
    ]
    _run(
        command,
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "first-stdout.log",
        stderr=setup_log_dir / "first-stderr.log",
    )
    result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["mode"] == "apply"
    assert result["qrexec_policy_changed"] == 1
    assert result["qrexec_policy_file"] == str(policy_file)
    assert policy_file.read_text(encoding="utf-8") == (
        "# Managed by Airlock setup-config.\n"
        "# Source: airlock.qubes.topology.qrexec_policy.\n"
        "# Bootstrap config transport policy remains in 30-airlock-config.policy.\n"
        "airlock.PolicyProbe * dom0 sys-config-vault allow\n"
        "airlock.PolicyProbe * @anyvm @anyvm deny\n"
    )
    systemctl_lines = systemctl_log.read_text(encoding="utf-8").splitlines()
    assert "systemctl reload qubes-qrexec-policy-daemon.service" in systemctl_lines
    assert "systemctl try-restart qubes-qrexec-policy-daemon.service" in systemctl_lines

    systemctl_log.write_text("", encoding="utf-8")
    _run(
        command,
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "second-stdout.log",
        stderr=setup_log_dir / "second-stderr.log",
    )
    result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["mode"] == "apply"
    assert result["qrexec_policy_changed"] == 0
    current = json.loads((state_dir / "current-setup.json").read_text(encoding="utf-8"))
    assert current["schema"] == "airlock.setup-config.v1"
    assert current["status"] == "passed"
    assert current["qrexec_policy_changed"] == 0
    current_topology = json.loads(
        (state_dir / "current-topology.json").read_text(encoding="utf-8")
    )
    assert current_topology["schema"] == "airlock.qubes-topology.v1"
    assert len(current_topology["qrexec_policy"]["rules"]) == 2
    assert systemctl_log.read_text(encoding="utf-8") == ""
    assert mutation_log.read_text(encoding="utf-8") == ""


def check_setup_config_restart_actions(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    kernel_dir = work_dir / "kernel/6.12.64-1.fc41"
    kernel_target = work_dir / "vm-kernels/6.12.64-1.fc41"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    state_dir = work_dir / "state"
    capabilities = work_dir / "xen-capabilities"
    mutation_log = work_dir / "mutations.log"
    qubes_xml = work_dir / "qubes.xml"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (kernel_dir, kernel_target, setup_log_dir, policy_dir, state_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    for name in ("vmlinuz", "initramfs", "modules.img"):
        (kernel_dir / name).write_text(f"{name}\n", encoding="utf-8")
        shutil.copy(kernel_dir / name, kernel_target / name)
    (kernel_target / "MANAGED_BY_AIRLOCK").write_text(
        f"source={kernel_dir}\nschema=airlock.qubes-kernel.v1\n", encoding="utf-8"
    )
    mutation_log.write_text("", encoding="utf-8")
    qubes_xml.write_text(
        """
        <qubes>
          <properties><property name="default_kernel"></property></properties>
          <domains><domain><properties>
            <property name="name">legacy-fedora-app</property>
            <property name="netvm"></property>
          </properties></domain></domains>
        </qubes>
        """,
        encoding="utf-8",
    )
    (policy_dir / "90-airlock.policy").write_text(
        "# Managed by Airlock setup-config.\n"
        "# Source: airlock.qubes.topology.qrexec_policy.\n"
        "# Bootstrap config transport policy remains in 30-airlock-config.policy.\n",
        encoding="utf-8",
    )
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {"6.12.64-1.fc41": {"source": str(kernel_dir)}},
                "domains": {
                    "legacy-fedora-app": {
                        "class": "AppVM",
                        "template": "fedora-44-xfce",
                        "label": "green",
                        "netvm": None,
                        "prefs": {"autostart": False, "memory": 1024},
                        "features": {"airlock-role": "legacy-fedora-app"},
                        "services": {"network-manager": False},
                    }
                },
                "prefs": {"default_kernel": ""},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commands = {
        "qvm-ls": """
        #!/usr/bin/env bash
        case "$*" in
          *"--fields NAME,STATE"*)
            printf 'dom0|Running\n'
            printf 'fedora-44-xfce|Halted\n'
            printf 'legacy-fedora-app|Running\n'
            ;;
          *)
            printf 'dom0\n'
            printf 'fedora-44-xfce\n'
            printf 'legacy-fedora-app\n'
            ;;
        esac
        """,
        "systemctl": """
        #!/usr/bin/env bash
        if [ "$1" = "is-active" ]; then
          exit 0
        fi
        if [ "$1" = "reload" ] || [ "$1" = "try-restart" ]; then
          printf 'systemctl %s\n' "$*" >> "$MUTATION_LOG"
          exit 0
        fi
        exit 1
        """,
        "qubes-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 1 ] && [ "$1" = "default_kernel" ]; then
          printf '\n'
          exit 0
        fi
        printf 'qubes-prefs %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
        "qvm-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          case "$1:$2" in
            legacy-fedora-app:label) printf 'green\n' ;;
            legacy-fedora-app:netvm) printf 'none\n' ;;
            legacy-fedora-app:autostart) printf 'False\n' ;;
            legacy-fedora-app:memory) printf '512\n' ;;
            *) exit 1 ;;
          esac
          exit 0
        fi
        printf 'qvm-prefs %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
        "qvm-features": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ] && [ "$1:$2" = "legacy-fedora-app:airlock-role" ]; then
          printf 'legacy-fedora-app\n'
          exit 0
        fi
        printf 'qvm-features %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
        "qvm-service": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ] && [ "$1:$2" = "legacy-fedora-app:network-manager" ]; then
          printf 'on\n'
          exit 0
        fi
        printf 'qvm-service %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
        "qubesd-query": "#!/usr/bin/env bash\nexit 0\n",
    }
    unexpected = """
    #!/usr/bin/env bash
    printf '%s %s\n' "$0" "$*" >> "$MUTATION_LOG"
    exit 99
    """
    for name in ("qvm-create", "qvm-template-postprocess", "qvm-volume"):
        commands[name] = unexpected
    _setup_fake_command_dir(bin_dir, commands)
    for command_name in (
        "awk",
        "chmod",
        "cmp",
        "cp",
        "grep",
        "jq",
        "mkdir",
        "python3",
        "rm",
    ):
        path = shutil.which(command_name)
        if path is not None and not (bin_dir / command_name).exists():
            (bin_dir / command_name).symlink_to(path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MUTATION_LOG": str(mutation_log),
            "AIRLOCK_QUBES_KERNEL_BASE": str(work_dir / "vm-kernels"),
            "AIRLOCK_QUBES_XML": str(qubes_xml),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(setup_log_dir),
        }
    )
    _run(
        [
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/setup-config.sh"),
            "--apply",
            "--topology",
            str(topology),
        ],
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "stdout.log",
        stderr=setup_log_dir / "stderr.log",
    )
    result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["mode"] == "apply"
    assert result["templates_changed"] == 0
    assert result["kernels_changed"] == 0
    assert result["domains_changed"] == 0
    assert result["prefs_changed"] == 1
    assert result["features_changed"] == 0
    assert result["services_changed"] == 1
    assert result["qrexec_policy_changed"] == 0
    assert result["restart_required"] is True
    assert len(result["live_actions"]) == 2
    assert len(result["restart_actions"]) == 2
    assert any(
        action["type"] == "domain-pref"
        and action["action"] == "set"
        and action["domain"] == "legacy-fedora-app"
        and action["field"] == "memory"
        and action["value"] == "1024"
        for action in result["live_actions"]
    )
    assert any(
        action["type"] == "domain-service"
        and action["action"] == "set"
        and action["domain"] == "legacy-fedora-app"
        and action["field"] == "network-manager"
        and action["value"] == "off"
        for action in result["live_actions"]
    )
    for field, value in (("memory", "1024"), ("network-manager", "off")):
        assert any(
            action["type"] in {"domain-pref", "domain-service"}
            and action["action"] == "restart"
            and action["domain"] == "legacy-fedora-app"
            and action["field"] == field
            and action["value"] == value
            and action["state"] == "Running"
            and action["reason"]
            == "domain is running; restart required for this change to fully take effect"
            for action in result["restart_actions"]
        )
    current = json.loads((state_dir / "current-setup.json").read_text(encoding="utf-8"))
    assert current["schema"] == "airlock.setup-config.v1"
    assert current["status"] == "passed"
    assert current["restart_required"] is True
    assert len(current["restart_actions"]) == 2
    current_topology = json.loads(
        (state_dir / "current-topology.json").read_text(encoding="utf-8")
    )
    assert current_topology["schema"] == "airlock.qubes-topology.v1"
    assert current_topology["domains"]["legacy-fedora-app"]["prefs"]["memory"] == 1024
    assert (
        current_topology["domains"]["legacy-fedora-app"]["services"]["network-manager"]
        is False
    )
    mutation_lines = mutation_log.read_text(encoding="utf-8").splitlines()
    assert "qvm-prefs legacy-fedora-app memory 1024" in mutation_lines
    assert "qvm-service legacy-fedora-app network-manager off" in mutation_lines
