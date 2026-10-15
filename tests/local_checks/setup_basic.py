from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from .support import _run, _setup_fake_command_dir


def check_setup_config_static(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    kernel_dir = work_dir / "kernel/6.12.64-1.fc41"
    template_dir = work_dir / "template/fedora-44-xfce"
    setup_log_dir = work_dir / "logs"
    kernel_base = work_dir / "vm-kernels"
    policy_dir = work_dir / "policy.d"
    capabilities = work_dir / "xen-capabilities"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (kernel_dir, template_dir, setup_log_dir, policy_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    for name in ("vmlinuz", "initramfs", "modules.img"):
        (kernel_dir / name).touch()
    (template_dir / "root.img").touch()
    (template_dir / "template.conf").write_text(
        "qrexec=1\nvirt-mode=pvh\n", encoding="utf-8"
    )
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {
                    "fedora-44-xfce": {
                        "import": True,
                        "skip_start": True,
                        "source": str(template_dir),
                    }
                },
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
    unexpected = """
    #!/usr/bin/env bash
    printf 'unexpected command execution: %s\n' "$0" >&2
    exit 97
    """
    commands = {
        name: unexpected
        for name in (
            "qubes-prefs",
            "qvm-appmenus",
            "qvm-create",
            "qvm-features",
            "qvm-prefs",
            "qvm-service",
            "qvm-template-postprocess",
            "qvm-volume",
            "qubesd-query",
            "runuser",
        )
    }
    commands["qvm-ls"] = "#!/usr/bin/env bash\nexit 0\n"
    commands["systemctl"] = """
    #!/usr/bin/env bash
    if [ "$1" = "is-active" ]; then
      exit 0
    fi
    exit 1
    """
    _setup_fake_command_dir(bin_dir, commands)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AIRLOCK_QUBES_KERNEL_BASE": str(kernel_base),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(setup_log_dir),
        }
    )
    _run(
        [
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/setup-config.sh"),
            "--plan",
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
    assert result["mode"] == "plan"
    assert result["templates_changed"] == 1
    assert result["kernels_changed"] == 1
    assert result["domains_changed"] == 1
    assert result["prefs_changed"] == 1
    assert result["qrexec_policy_changed"] == 1


def check_setup_config_empty_global_pref(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    state_dir = work_dir / "state"
    capabilities = work_dir / "xen-capabilities"
    mutation_log = work_dir / "mutations.log"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (setup_log_dir, policy_dir, state_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    mutation_log.write_text("", encoding="utf-8")
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {},
                "prefs": {"default_kernel": ""},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commands = {
        "systemctl": """
        #!/usr/bin/env bash
        if [ "$1" = "is-active" ]; then
          exit 0
        fi
        if [ "$1" = "reload" ] || [ "$1" = "try-restart" ]; then
          exit 0
        fi
        exit 1
        """,
        "qvm-ls": "#!/usr/bin/env bash\nprintf 'dom0\n'\n",
        "qubes-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 1 ] && [ "$1" = "default_kernel" ]; then
          printf '\n'
          exit 0
        fi
        {
          printf 'qubes-prefs'
          for arg in "$@"; do
            printf ' <%s>' "$arg"
          done
          printf '\n'
        } >> "$MUTATION_LOG"
        """,
    }
    unexpected = """
    #!/usr/bin/env bash
    printf 'unexpected command execution: %s\n' "$0" >&2
    exit 97
    """
    for name in (
        "qvm-create",
        "qvm-features",
        "qvm-prefs",
        "qvm-service",
        "qvm-template-postprocess",
        "qvm-volume",
        "qubesd-query",
    ):
        commands[name] = unexpected
    _setup_fake_command_dir(bin_dir, commands)
    for command in ("cmp", "grep", "jq"):
        path = shutil.which(command)
        if path is not None:
            (bin_dir / command).symlink_to(path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MUTATION_LOG": str(mutation_log),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
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
    assert result["prefs_changed"] == 1
    assert result["qrexec_policy_changed"] == 1
    current = json.loads((state_dir / "current-setup.json").read_text(encoding="utf-8"))
    assert current["schema"] == "airlock.setup-config.v1"
    assert current["status"] == "passed"
    assert current["prefs_changed"] == 1
    assert (
        "qubes-prefs <default_kernel> <>"
        in mutation_log.read_text(encoding="utf-8").splitlines()
    )


def check_setup_config_pref_order(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    state_dir = work_dir / "state"
    capabilities = work_dir / "xen-capabilities"
    mutation_log = work_dir / "mutations.log"
    memory_marker = work_dir / "memory-set"
    qubes_xml = work_dir / "qubes.xml"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (setup_log_dir, policy_dir, state_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    mutation_log.write_text("", encoding="utf-8")
    qubes_xml.write_text(
        """
        <qubes><domains><domain><properties>
          <property name="name">sys-config-staging</property>
          <property name="netvm"></property>
        </properties></domain></domains></qubes>
        """,
        encoding="utf-8",
    )
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {
                    "sys-config-staging": {
                        "class": "AppVM",
                        "template": "fedora-44-xfce",
                        "label": "gray",
                        "netvm": None,
                        "prefs": {
                            "autostart": False,
                            "maxmem": 4096,
                            "memory": 2048,
                            "vcpus": 2,
                        },
                        "features": {},
                        "services": {},
                    }
                },
                "prefs": {},
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
          *NAME,STATE*)
            printf 'dom0|Running\nconfig-staging|Halted\n'
            ;;
          *)
            printf 'dom0\nconfig-staging\n'
            ;;
        esac
        """,
        "qvm-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          case "$1:$2" in
            sys-config-staging:label) printf 'gray\n' ;;
            sys-config-staging:netvm) printf 'none\n' ;;
            sys-config-staging:autostart) printf 'True\n' ;;
            sys-config-staging:memory) printf '400\n' ;;
            sys-config-staging:maxmem) printf '4000\n' ;;
            sys-config-staging:vcpus) printf '1\n' ;;
            *) exit 1 ;;
          esac
          exit 0
        fi
        if [ "$#" -eq 3 ] && [ "$1" = "sys-config-staging" ]; then
          if [ "$2" = "maxmem" ] && [ ! -e "$MEMORY_SET_MARKER" ]; then
            printf 'maxmem was set before memory\n' >&2
            exit 42
          fi
          if [ "$2" = "memory" ]; then
            : > "$MEMORY_SET_MARKER"
          fi
          printf 'qvm-prefs %s\n' "$*" >> "$MUTATION_LOG"
          exit 0
        fi
        exit 1
        """,
        "systemctl": """
        #!/usr/bin/env bash
        case "$1" in
          is-active|reload|try-restart)
            exit 0
            ;;
        esac
        exit 1
        """,
    }
    for name in (
        "qubes-prefs",
        "qvm-create",
        "qvm-features",
        "qvm-service",
        "qvm-template-postprocess",
        "qvm-volume",
        "qubesd-query",
    ):
        commands[name] = "#!/usr/bin/env bash\nexit 0\n"
    _setup_fake_command_dir(bin_dir, commands)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MEMORY_SET_MARKER": str(memory_marker),
            "MUTATION_LOG": str(mutation_log),
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
    command_lines = (
        (setup_log_dir / "setup-config.commands.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert command_lines.index("+ qvm-prefs sys-config-staging memory 2048") < (
        command_lines.index("+ qvm-prefs sys-config-staging maxmem 4096")
    )
    result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["prefs_changed"] == 4


def check_setup_config_external_netvm(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    capabilities = work_dir / "xen-capabilities"
    mutation_log = work_dir / "mutations.log"
    topology = work_dir / "topology.json"
    shutil.rmtree(work_dir, ignore_errors=True)
    setup_log_dir.mkdir(parents=True)
    policy_dir.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    mutation_log.write_text("", encoding="utf-8")
    topology.write_text(
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
                        "prefs": {"autostart": False},
                        "features": {"airlock-role": "dev"},
                        "services": {},
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
          "--raw-data --fields NAME --no-spinner")
            printf 'dom0\n'
            printf 'dev\n'
            ;;
          "--raw-data --fields NAME,STATE --no-spinner")
            printf 'dom0|Running\n'
            printf 'dev|Halted\n'
            ;;
          "--raw-data --fields NAME,CLASS --no-spinner")
            printf 'dom0|AdminVM\n'
            printf 'dev|StandaloneVM\n'
            ;;
          *)
            printf 'unexpected qvm-ls args: %s\n' "$*" >&2
            exit 1
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
        "qvm-create": """
        #!/usr/bin/env bash
        printf 'qvm-create %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
        "qvm-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          case "$1:$2" in
            dev:label) printf 'green\n' ;;
            dev:autostart) printf 'False\n' ;;
            *) exit 1 ;;
          esac
          exit 0
        fi
        if [ "$#" -eq 3 ] && [ "$1:$2:$3" = "dev:netvm:sys-firewall" ]; then
          printf 'setup-config should defer missing external sys-firewall instead of setting it\n' >&2
          exit 97
        fi
        printf 'qvm-prefs %s\n' "$*" >> "$MUTATION_LOG"
        exit 0
        """,
    }
    for name in (
        "qvm-appmenus",
        "qvm-features",
        "qvm-service",
        "qvm-template-postprocess",
        "qvm-volume",
        "qubesd-query",
        "runuser",
    ):
        commands[name] = """
        #!/usr/bin/env bash
        printf '%s %s\n' "$(basename "$0")" "$*" >> "$MUTATION_LOG"
        exit 0
        """
    _setup_fake_command_dir(bin_dir, commands)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MUTATION_LOG": str(mutation_log),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_SETUP_CONFIG_PERSIST_STATE": "0",
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
    assert result["domains_changed"] == 0
    assert result["prefs_changed"] == 1
    assert any(
        action["type"] == "domain-pref"
        and action["action"] == "defer"
        and action["domain"] == "dev"
        and action["field"] == "netvm"
        and action["value"] == "sys-firewall"
        for action in result["live_actions"]
    )
    assert (
        "qvm-prefs dev netvm sys-firewall"
        not in mutation_log.read_text(encoding="utf-8").splitlines()
    )


def check_setup_config_idempotent(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    kernel_dir = work_dir / "kernel/6.12.64-1.fc41"
    kernel_base = work_dir / "vm-kernels"
    kernel_target = kernel_base / "6.12.64-1.fc41"
    template_dir = work_dir / "template/fedora-44-xfce"
    topology_path = work_dir / "topology.json"
    capabilities = work_dir / "xen-capabilities"
    setup_log_dir = work_dir / "logs"
    mutation_log = work_dir / "mutations.log"
    qubes_xml = work_dir / "qubes.xml"
    policy_dir = work_dir / "policy.d"
    policy_file = policy_dir / "90-airlock.policy"
    state_dir = work_dir / "state"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        kernel_dir,
        kernel_target,
        template_dir,
        setup_log_dir,
        policy_dir,
        state_dir,
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    for filename in ("vmlinuz", "initramfs", "modules.img"):
        (kernel_dir / filename).write_text(filename + "\n", encoding="utf-8")
        shutil.copy2(kernel_dir / filename, kernel_target / filename)
    (kernel_target / "MANAGED_BY_AIRLOCK").write_text(
        f"source={kernel_dir}\nschema=airlock.qubes-kernel.v1\n",
        encoding="utf-8",
    )
    (template_dir / "root.img").touch()
    (template_dir / "template.conf").write_text(
        "qrexec=1\nvirt-mode=pvh\n", encoding="utf-8"
    )
    mutation_log.touch()
    qubes_xml.write_text(
        """
        <qubes>
          <properties>
            <property name="default_kernel"></property>
          </properties>
          <domains><domain><properties>
            <property name="name">legacy-fedora-app</property>
            <property name="netvm"></property>
          </properties></domain></domains>
        </qubes>
        """,
        encoding="utf-8",
    )
    policy_file.write_text(
        "\n".join(
            (
                "# Managed by Airlock setup-config.",
                "# Source: airlock.qubes.topology.qrexec_policy.",
                "# Bootstrap config transport policy remains in 30-airlock-config.policy.",
                "",
            )
        ),
        encoding="utf-8",
    )
    topology_path.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {
                    "fedora-44-xfce": {
                        "import": True,
                        "skip_start": True,
                        "source": str(template_dir),
                    }
                },
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
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _setup_fake_command_dir(
        bin_dir,
        {
            "qvm-ls": """
                #!/usr/bin/env bash
                printf 'dom0\\nfedora-44-xfce\\nlegacy-fedora-app\\n'
            """,
            "systemctl": """
                #!/usr/bin/env bash
                if [ "$1" = "is-active" ]; then
                  exit 0
                fi
                if [ "$1" = "reload" ] || [ "$1" = "try-restart" ]; then
                  printf 'systemctl %s\\n' "$*" >> "$MUTATION_LOG"
                  exit 0
                fi
                exit 1
            """,
            "qubes-prefs": """
                #!/usr/bin/env bash
                if [ "$#" -eq 1 ] && [ "$1" = "default_kernel" ]; then
                  printf '\\n'
                  exit 0
                fi
                printf 'qubes-prefs %s\\n' "$*" >> "$MUTATION_LOG"
                exit 0
            """,
            "qvm-prefs": """
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ]; then
                  case "$1:$2" in
                    legacy-fedora-app:label) printf 'green\\n' ;;
                    legacy-fedora-app:netvm) printf 'none\\n' ;;
                    legacy-fedora-app:autostart) printf 'False\\n' ;;
                    legacy-fedora-app:memory) printf '1024\\n' ;;
                    *) exit 1 ;;
                  esac
                  exit 0
                fi
                printf 'qvm-prefs %s\\n' "$*" >> "$MUTATION_LOG"
                exit 0
            """,
            "qvm-features": """
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ] && [ "$1:$2" = "legacy-fedora-app:airlock-role" ]; then
                  printf 'legacy-fedora-app\\n'
                  exit 0
                fi
                printf 'qvm-features %s\\n' "$*" >> "$MUTATION_LOG"
                exit 0
            """,
            "qvm-service": """
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ] && [ "$1:$2" = "legacy-fedora-app:network-manager" ]; then
                  printf 'off\\n'
                  exit 0
                fi
                printf 'qvm-service %s\\n' "$*" >> "$MUTATION_LOG"
                exit 0
            """,
            "qvm-create": """
                #!/usr/bin/env bash
                printf '%s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 99
            """,
            "qvm-template-postprocess": """
                #!/usr/bin/env bash
                printf '%s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 99
            """,
            "qvm-volume": """
                #!/usr/bin/env bash
                printf '%s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 99
            """,
            "qubesd-query": """
                #!/usr/bin/env bash
                exit 0
            """,
        },
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "MUTATION_LOG": str(mutation_log),
            "AIRLOCK_QUBES_KERNEL_BASE": str(kernel_base),
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
            str(topology_path),
        ],
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "stdout.log",
        stderr=setup_log_dir / "stderr.log",
    )
    setup = json.loads((setup_log_dir / "setup-config.json").read_text())
    assert setup["schema"] == "airlock.setup-config.v1"
    assert setup["status"] == "passed"
    assert setup["mode"] == "apply"
    for key in (
        "templates_changed",
        "kernels_changed",
        "domains_changed",
        "prefs_changed",
        "features_changed",
        "services_changed",
        "qrexec_policy_changed",
    ):
        assert setup[key] == 0
    current_setup = json.loads((state_dir / "current-setup.json").read_text())
    assert current_setup["schema"] == "airlock.setup-config.v1"
    assert current_setup["status"] == "passed"
    assert current_setup["restart_required"] is False
    assert current_setup["restart_actions"] == []
    current_topology = json.loads((state_dir / "current-topology.json").read_text())
    assert current_topology["schema"] == "airlock.qubes-topology.v1"
    assert current_topology["domains"]["legacy-fedora-app"]["prefs"]["memory"] == 1024
    assert mutation_log.read_text(encoding="utf-8") == ""
