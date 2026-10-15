from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from .support import _physical_store_path, _run, _setup_fake_command_dir, _write_executable


def check_setup_config_auto_sys_net_pci(
    repo_root: Path,
    log_dir: Path,
    *,
    domain_name: str = "sys-net",
    default_route: str = "00_03.0",
    exclusions: str = "",
    expected_devices: tuple[str, ...] = ("00_04.0",),
) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    capabilities = work_dir / "xen-capabilities"
    topology = work_dir / "topology.json"
    qubesadmin_python = work_dir / "qubesadmin-python"
    qubesadmin_python_log = work_dir / "qubesadmin-python.log"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (setup_log_dir, policy_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    qubesadmin_python_log.write_text("", encoding="utf-8")
    _write_executable(
        qubesadmin_python,
        """
        #!/usr/bin/env bash
        cat >> "$QUBESADMIN_PYTHON_LOG"
        printf '00_03.0\n00_04.0\n'
        """,
    )
    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {
                    "sys-net": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "red",
                        "netvm": None,
                        "prefs": {
                            "autostart": False,
                            "provides_network": True,
                        },
                        "features": {"airlock-role": "sys-net"},
                        "services": {
                            "network-manager": True,
                            "qubes-firewall": True,
                            "qubes-network": True,
                        },
                        "devices": {"pci": []},
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
          "--raw-data --fields NAME --no-spinner")
            printf 'dom0\nsys-net\n'
            ;;
          "--raw-data --fields NAME,STATE --no-spinner")
            printf 'dom0|Running\nsys-net|Halted\n'
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
        exit 1
        """,
        "ip": "#!/usr/bin/env bash\nexit 0\n",
        "readlink": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name in (
        "qubes-prefs",
        "qvm-create",
        "qvm-device",
        "qvm-features",
        "qvm-prefs",
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
            "AIRLOCK_DOM0_DEFAULT_ROUTE_PCI_DEVICES": default_route,
            "AIRLOCK_SYS_NET_PCI_EXCLUDE": exclusions,
            "AIRLOCK_QUBESADMIN_PYTHON": str(qubesadmin_python),
            "QUBESADMIN_PYTHON_LOG": str(qubesadmin_python_log),
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(setup_log_dir),
        }
    )
    env.pop("AIRLOCK_DOM0_NETWORK_PCI_DEVICES", None)
    if domain_name != "sys-net":
        definition = json.loads(topology.read_text())
        domain = definition["domains"].pop("sys-net")
        domain["features"] = {
            "airlock-role": domain_name,
            "airlock-auto-network-pci": "1",
        }
        definition["domains"][domain_name] = domain
        topology.write_text(json.dumps(definition))
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
    command_lines = (
        (setup_log_dir / "setup-config.commands.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["mode"] == "plan"
    assert result["devices_changed"] == len(expected_devices)
    assignments = {
        action["value"]
        for action in result["live_actions"]
        if action["type"] == "domain-device"
        and action["action"] == "assign"
        and action["domain"] == domain_name
        and action["field"] == "pci"
    }
    assert assignments == {
        f"dom0:{device} mode=required match=port" for device in expected_devices
    }
    for device in expected_devices:
        assert (
            f"+ qvm-device pci assign --required --port {domain_name} dom0:{device}"
            in command_lines
        )
    qubesadmin_script = qubesadmin_python_log.read_text(encoding="utf-8")
    assert "import qubesadmin" in qubesadmin_script


def check_setup_config_sys_net_order(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    state_dir = work_dir / "state"
    capabilities = work_dir / "xen-capabilities"
    qubes_xml = work_dir / "qubes.xml"
    topology = work_dir / "topology.json"
    mutation_log = work_dir / "mutations.log"
    created_domains = work_dir / "created-domains"
    status_device = work_dir / "bootstrap-status"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (setup_log_dir, policy_dir, state_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    mutation_log.write_text("", encoding="utf-8")
    created_domains.write_text("", encoding="utf-8")
    status_device.write_text("", encoding="utf-8")
    qubes_xml.write_text(
        """
        <qubes>
          <domains>
            <domain id="domain-1" class="StandaloneVM">
              <properties>
                <property name="name">sys-net</property>
              </properties>
            </domain>
          </domains>
        </qubes>
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
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "gray",
                        "netvm": None,
                        "prefs": {"autostart": False, "guivm": None},
                        "features": {},
                        "services": {},
                    },
                    "dev": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "green",
                        "netvm": "sys-firewall",
                        "prefs": {"autostart": False},
                        "features": {},
                        "services": {},
                    },
                    "sys-firewall": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "green",
                        "netvm": "sys-net",
                        "prefs": {
                            "autostart": True,
                            "guivm": None,
                            "memory": 500,
                            "provides_network": True,
                        },
                        "features": {
                            "airlock-role": "sys-firewall",
                            "servicevm": "1",
                        },
                        "services": {
                            "network-manager": False,
                            "qubes-firewall": True,
                            "qubes-network": True,
                            "qubes-updates-proxy": True,
                        },
                        "devices": {"pci": []},
                    },
                    "sys-gui": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "black",
                        "netvm": None,
                        "prefs": {
                            "autostart": True,
                            "guivm": "dom0",
                        },
                        "features": {"airlock-role": "sys-gui"},
                        "services": {
                            "guivm": True,
                            "guivm-gui-agent": True,
                            "network-manager": False,
                        },
                        "devices": {"pci": []},
                    },
                    "sys-net": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "red",
                        "netvm": None,
                        "prefs": {
                            "autostart": True,
                            "guivm": None,
                            "provides_network": True,
                        },
                        "features": {"airlock-role": "sys-net"},
                        "services": {
                            "network-manager": True,
                            "qubes-firewall": True,
                            "qubes-network": True,
                            "qubes-updates-proxy": True,
                        },
                        "devices": {"pci": []},
                    },
                },
                "prefs": {
                    "default_guivm": "sys-gui",
                    "default_netvm": "sys-firewall",
                },
                "qrexec_policy": {"rules": []},
            },
            sort_keys=True,
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
            cat "$CREATED_DOMAINS"
            ;;
          "--raw-data --fields NAME,STATE --no-spinner")
            printf 'dom0|Running\n'
            while IFS= read -r domain; do
              [ -n "$domain" ] || continue
              printf '%s|Halted\n' "$domain"
            done < "$CREATED_DOMAINS"
            ;;
          "--raw-data --fields NAME,CLASS --no-spinner")
            printf 'dom0|AdminVM\n'
            while IFS= read -r domain; do
              [ -n "$domain" ] || continue
              printf '%s|StandaloneVM\n' "$domain"
            done < "$CREATED_DOMAINS"
            ;;
          *)
            printf 'unexpected qvm-ls args: %s\n' "$*" >&2
            exit 1
            ;;
        esac
        """,
        "qvm-create": """
        #!/usr/bin/env bash
        printf 'qvm-create %s\n' "$*" >> "$MUTATION_LOG"
        printf '%s\n' "${!#}" >> "$CREATED_DOMAINS"
        """,
        "qvm-device": """
        #!/usr/bin/env bash
        if [ "$1" = "pci" ] && [ "$2" = "list" ]; then
          exit 0
        fi
        printf 'qvm-device %s\n' "$*" >> "$MUTATION_LOG"
        """,
        "qvm-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          if [ "$1" = "sys-net" ] && [ "$2" = "netvm" ]; then
            printf 'none\n'
            exit 0
          fi
          if [ "$1" = "sys-gui" ] && [ "$2" = "guivm" ]; then
            printf 'dom0\n'
            exit 0
          fi
          exit 1
        fi
        printf 'qvm-prefs %s\n' "$*" >> "$MUTATION_LOG"
        """,
        "qvm-features": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          exit 1
        fi
        printf 'qvm-features %s\n' "$*" >> "$MUTATION_LOG"
        """,
        "qvm-service": """
        #!/usr/bin/env bash
        if [ "$#" -eq 2 ]; then
          exit 1
        fi
        printf 'qvm-service %s\n' "$*" >> "$MUTATION_LOG"
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
        "qubes-prefs": """
        #!/usr/bin/env bash
        if [ "$#" -eq 1 ]; then
          exit 1
        fi
        printf 'qubes-prefs %s\n' "$*" >> "$MUTATION_LOG"
        """,
    }
    for name in (
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
            "CREATED_DOMAINS": str(created_domains),
            "MUTATION_LOG": str(mutation_log),
            "AIRLOCK_DOM0_NETWORK_PCI_DEVICES": "00_02.0 00_03.0",
            "AIRLOCK_DOM0_DEFAULT_ROUTE_PCI_DEVICES": "00_02.0",
            "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
            "AIRLOCK_QUBES_XML": str(qubes_xml),
            "AIRLOCK_SETUP_CONFIG_STATUS_DEVICE": str(status_device),
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
            "--boot-critical",
            "--topology",
            str(topology),
        ],
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "stdout.log",
        stderr=setup_log_dir / "stderr.log",
    )
    boot_command_lines = (
        (setup_log_dir / "setup-config.commands.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    boot_result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert boot_result["scope"] == "boot-critical"
    assert not any("sys-config-staging" in line for line in boot_command_lines)
    assert not any(" dev" in line for line in boot_command_lines)
    assert "+ qvm-prefs sys-net guivm none" in boot_command_lines
    assert "+ qvm-prefs sys-firewall guivm none" in boot_command_lines
    assert "+ qvm-prefs sys-gui guivm dom0" in boot_command_lines
    assert "+ qvm-prefs sys-gui netvm none" in boot_command_lines
    completed_screen = status_device.read_text(encoding="utf-8")
    assert "Airlock | Startup" in completed_screen
    assert "Topology configured; verifying required VMs next" in completed_screen
    assert "3 startup VMs prepared." in completed_screen
    assert completed_screen.count("[done]") == 4
    assert "[working]" not in completed_screen
    assert "Elapsed:" in completed_screen
    assert "Startup VM configuration is ready; the desktop is starting next." in completed_screen
    assert "Required hardware and VM checks still need to finish." in completed_screen

    _run(
        [
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/setup-config.sh"),
            "--apply",
            "--deferred",
            "--topology",
            str(topology),
        ],
        cwd=repo_root,
        env=env,
        stdout=setup_log_dir / "deferred-stdout.log",
        stderr=setup_log_dir / "deferred-stderr.log",
    )
    # Deferred setup and the stopped timer must not overwrite the handover screen.
    assert status_device.read_text(encoding="utf-8") == completed_screen
    deferred_command_lines = (
        (setup_log_dir / "setup-config.commands.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    sys_net_create = boot_command_lines.index(
        "+ qvm-create --class StandaloneVM --label red sys-net"
    )
    sys_firewall_create = boot_command_lines.index(
        "+ qvm-create --class StandaloneVM --label green sys-firewall"
    )
    sys_gui_create = boot_command_lines.index(
        "+ qvm-create --class StandaloneVM --label black sys-gui"
    )
    config_create = deferred_command_lines.index(
        "+ qvm-create --class StandaloneVM --label gray sys-config-staging"
    )
    dev_create = deferred_command_lines.index(
        "+ qvm-create --class StandaloneVM --label green dev"
    )
    sys_net_netvm = boot_command_lines.index("+ qvm-prefs sys-net netvm none")
    sys_net_pci = boot_command_lines.index(
        "+ qvm-device pci assign --required --port sys-net dom0:00_03.0"
    )
    sys_firewall_netvm = boot_command_lines.index(
        "+ qvm-prefs sys-firewall netvm sys-net"
    )
    sys_gui_guivm = boot_command_lines.index("+ qvm-prefs sys-gui guivm dom0")
    sys_gui_netvm = boot_command_lines.index("+ qvm-prefs sys-gui netvm none")
    default_guivm = boot_command_lines.index("+ qubes-prefs default_guivm sys-gui")
    assert "+ qvm-prefs dev netvm sys-firewall" in deferred_command_lines
    assert "+ qvm-prefs sys-config-staging guivm none" in deferred_command_lines
    assert "+ qubes-prefs default_netvm sys-firewall" in boot_command_lines

    assert sys_net_create < sys_firewall_create
    assert sys_gui_create < sys_gui_guivm
    assert sys_gui_create < sys_gui_netvm
    assert sys_gui_guivm < default_guivm
    assert sys_gui_netvm < default_guivm
    assert sys_net_netvm < sys_net_pci
    assert sys_net_pci < sys_firewall_netvm
    assert config_create < dev_create

    result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.setup-config.v1"
    assert result["status"] == "passed"
    assert result["scope"] == "deferred"
    assert boot_result["domains_changed"] == 3
    assert boot_result["devices_changed"] == 1
    assert result["domains_changed"] == 2
    assert result["devices_changed"] == 0

    # Minimal GUI/USB images use the full scope at boot. A supplied status
    # device must show progress/completion there too, rather than stay blank.
    status_device.write_text("", encoding="utf-8")
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
        stdout=setup_log_dir / "all-stdout.log",
        stderr=setup_log_dir / "all-stderr.log",
    )
    full_result = json.loads(
        (setup_log_dir / "setup-config.json").read_text(encoding="utf-8")
    )
    assert full_result["scope"] == "all"
    assert full_result["status"] == "passed"
    full_screen = status_device.read_text(encoding="utf-8")
    assert "Topology configured; verifying required VMs next" in full_screen
    assert "5 startup VMs prepared." in full_screen
    assert full_screen.count("[done]") == 4


def check_start_network_providers(repo_root: Path, log_dir: Path) -> None:
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    topology = log_dir / "topology.json"
    qvm_start = log_dir / "qvm-start"
    qvm_start_log = log_dir / "qvm-start.log"
    qvm_start_log.write_text("", encoding="utf-8")
    start_ready = log_dir / "airlock-start-qube-ready"

    out = _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.airlock-start-network-providers",
            "--no-link",
            "--print-out-paths",
        ],
        cwd=repo_root,
    ).stdout.strip()
    wrapper = (
        Path(_physical_store_path(repo_root, out))
        / "bin/airlock-start-network-providers"
    )

    topology.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "domains": {
                    "sys-net": {
                        "backend": {"system": {"source": "/nix/store/sys-net"}},
                        "netvm": None,
                        "prefs": {
                            "autostart": True,
                            "provides_network": True,
                        },
                    },
                    "sys-firewall": {
                        "backend": {"system": {"source": "/nix/store/sys-firewall"}},
                        "netvm": "sys-net",
                        "prefs": {
                            "autostart": True,
                            "provides_network": True,
                        },
                    },
                    "sys-vpn": {
                        "backend": {"system": {"source": "/nix/store/sys-vpn"}},
                        "netvm": None,
                        "prefs": {
                            "autostart": False,
                            "provides_network": True,
                        },
                    },
                    "dev": {
                        "backend": {"system": {"source": "/nix/store/dev"}},
                        "netvm": "sys-net",
                        "prefs": {
                            "autostart": True,
                            "provides_network": False,
                        },
                    },
                    "sys-none": {
                        "backend": {"system": {"source": "/nix/store/sys-none"}},
                        "netvm": "none",
                        "prefs": {
                            "autostart": True,
                            "provides_network": True,
                        },
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_executable(
        qvm_start,
        """
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> "$QVM_START_LOG"
        """,
    )
    _write_executable(
        start_ready,
        """
        #!/usr/bin/env bash
        "$AIRLOCK_QVM_START" --skip-if-running -- "$1"
        """,
    )
    env = os.environ.copy()
    env.update(
        {
            "AIRLOCK_QUBES_TOPOLOGY": str(topology),
            "AIRLOCK_QVM_START": str(qvm_start),
            "AIRLOCK_START_QUBE_READY": str(start_ready),
            "AIRLOCK_LOG_DIR": str(log_dir),
            "QVM_START_LOG": str(qvm_start_log),
        }
    )
    _run(
        [str(wrapper)],
        cwd=repo_root,
        env=env,
        stdout=log_dir / "stdout.log",
        stderr=log_dir / "stderr.log",
    )

    result = json.loads(
        (log_dir / "start-network-providers.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.start-network-providers.v1"
    assert result["status"] == "passed"
    assert result["providers"] == ["sys-net", "sys-firewall", "sys-none"]
    assert result["started_or_running"] == ["sys-net", "sys-firewall", "sys-none"]
    assert result["failed"] == []
    assert qvm_start_log.read_text(encoding="utf-8").splitlines() == [
        "--skip-if-running -- sys-net",
        "--skip-if-running -- sys-firewall",
        "--skip-if-running -- sys-none",
    ]
    command_log = (log_dir / "start-network-providers.commands.log").read_text(
        encoding="utf-8"
    )
    command_lines = [
        line
        for line in command_log.splitlines()
        if line.startswith("+ airlock-start-qube-ready ")
    ]
    assert command_lines == [
        f"+ airlock-start-qube-ready sys-net {topology}",
        f"+ airlock-start-qube-ready sys-firewall {topology}",
        f"+ airlock-start-qube-ready sys-none {topology}",
    ]
