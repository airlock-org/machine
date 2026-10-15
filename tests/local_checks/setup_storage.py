from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from .support import _physical_path_list, _physical_store_path, _run, _run_store_path, _setup_fake_command_dir, _write_executable


def check_setup_config_nix_native_running_storage(
    repo_root: Path, log_dir: Path
) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    topology_path = work_dir / "topology.json"
    capabilities = work_dir / "xen-capabilities"
    setup_log_dir = work_dir / "logs"
    policy_dir = work_dir / "policy.d"
    policy_file = policy_dir / "90-airlock.policy"
    state_dir = work_dir / "state"
    mutation_log = work_dir / "mutations.log"
    qubes_xml = work_dir / "qubes.xml"
    old_system_source = work_dir / "old/system-source"
    old_root_source = work_dir / "old/root-source"
    old_store_source = work_dir / "old/store-source"
    new_system_source = work_dir / "new/system-source"
    new_root_source = work_dir / "new/root-source"
    new_store_source = work_dir / "new/store-source"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        setup_log_dir,
        policy_dir,
        state_dir,
        old_system_source,
        old_root_source,
        old_store_source,
        new_system_source,
        new_root_source,
        new_store_source,
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    for source in (old_system_source, new_system_source):
        _write_executable(
            source / "init",
            """
            #!/bin/sh
            exec /init
            """,
        )
    (old_root_source / "root.img").write_text("old-root\n", encoding="utf-8")
    (new_root_source / "root.img").write_text("new-root\n", encoding="utf-8")
    (old_store_source / "store-paths").write_text(
        str(old_system_source) + "\n", encoding="utf-8"
    )
    (new_store_source / "store-paths").write_text(
        str(new_system_source) + "\n", encoding="utf-8"
    )
    for store_source, name in (
        (old_store_source, "old-store"),
        (new_store_source, "new-store"),
    ):
        (store_source / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "airlock.store-closure-manifest.v1",
                    "name": name,
                    "read_only": True,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    mutation_log.touch()
    qubes_xml.write_text(
        """
        <qubes><domains><domain><properties>
          <property name="name">sys-config-staging</property>
          <property name="netvm"></property>
        </properties></domain></domains></qubes>
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
                "templates": {},
                "kernels": {},
                "domains": {
                    "sys-config-staging": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "green",
                        "netvm": None,
                        "prefs": {
                            "autostart": False,
                            "memory": 1024,
                            "virt_mode": "pvh",
                        },
                        "features": {
                            "airlock-managed": "1",
                            "airlock-role": "sys-config-staging",
                            "qrexec": "1",
                        },
                        "services": {},
                        "backend": {
                            "type": "nix-closure",
                            "template": None,
                            "system": {
                                "source": str(new_system_source),
                                "source_ref": None,
                            },
                            "kernel": {
                                "name": "old-kernel",
                                "source": None,
                                "source_ref": None,
                            },
                            "initrd": {
                                "name": None,
                                "source": None,
                                "source_ref": None,
                            },
                            "modules": {
                                "name": None,
                                "source": None,
                                "source_ref": None,
                            },
                            "root": {
                                "mode": "qubes-root-image",
                                "source": str(new_root_source),
                                "source_ref": None,
                                "read_only": False,
                            },
                            "store": {
                                "mode": "shared-closure",
                                "source": str(new_store_source),
                                "source_ref": None,
                                "view_backend": "erofs-objectmap",
                                "mountpoint": "/nix/store",
                                "blob_device": "/dev/xvdf",
                                "device": "/dev/xvde",
                                "read_only": True,
                            },
                            "state": {
                                "persistent": "config-staging-private",
                                "volatile": None,
                            },
                        },
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
    _setup_fake_command_dir(
        bin_dir,
        {
            "qvm-ls": """
                #!/usr/bin/env bash
                case "$*" in
                  *"--fields NAME,STATE"*)
                    printf 'sys-config-staging|Running\\n'
                    ;;
                  *"--fields NAME,CLASS"*)
                    printf 'sys-config-staging|StandaloneVM\\n'
                    ;;
                  *)
                    printf 'sys-config-staging\\n'
                    ;;
                esac
            """,
            "qvm-prefs": f"""
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ]; then
                  case "$1:$2" in
                    sys-config-staging:label) printf 'green\\n' ;;
                    sys-config-staging:netvm) printf 'none\\n' ;;
                    sys-config-staging:autostart) printf 'False\\n' ;;
                    sys-config-staging:memory) printf '1024\\n' ;;
                    sys-config-staging:virt_mode) printf 'pvh\\n' ;;
                    sys-config-staging:kernel) printf 'old-kernel\\n' ;;
                    sys-config-staging:kernelopts) printf 'console=hvc0 init=/sbin/init airlock.system={old_system_source} airlock.storedev=/dev/xvde airlock.storefmt=erofs airlock.storeblobdev=/dev/xvdf\\n' ;;
                    *) exit 1 ;;
                  esac
                  exit 0
                fi
                printf 'qvm-prefs %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qvm-features": f"""
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ]; then
                  case "$1:$2" in
                    sys-config-staging:airlock-backend) printf 'nix-closure\\n' ;;
                    sys-config-staging:airlock-system-source) printf '{old_system_source}\\n' ;;
                    sys-config-staging:airlock-root-source) printf '{old_root_source}\\n' ;;
                    sys-config-staging:airlock-store-source) printf '{old_store_source}\\n' ;;
                    sys-config-staging:airlock-store-mode) printf 'shared-closure\\n' ;;
                    sys-config-staging:airlock-store-view-backend) printf 'erofs-objectmap\\n' ;;
                    sys-config-staging:airlock-store-generation) printf '1\\n' ;;
                    sys-config-staging:airlock-store-closure-hash) printf 'old-closure\\n' ;;
                    sys-config-staging:airlock-store-view-image) printf '/var/lib/airlock/closure-store/domains/sys-config-staging/generations/1/store.erofs\\n' ;;
                    sys-config-staging:airlock-store-filesystem) printf 'erofs\\n' ;;
                    sys-config-staging:airlock-store-blob-map) printf '/var/lib/airlock/closure-store/domains/sys-config-staging/generations/1/store.blob-map.json\\n' ;;
                    sys-config-staging:airlock-store-blob-volume) printf 'store-data\\n' ;;
                    sys-config-staging:airlock-store-blob-device) printf '/dev/xvdf\\n' ;;
                    sys-config-staging:airlock-store-mountpoint) printf '/nix/store\\n' ;;
                    sys-config-staging:airlock-store-device) printf '/dev/xvde\\n' ;;
                    sys-config-staging:airlock-store-read-only) printf 'True\\n' ;;
                    sys-config-staging:airlock-managed) printf '1\\n' ;;
                    sys-config-staging:airlock-role) printf 'sys-config-staging\\n' ;;
                    sys-config-staging:qrexec) printf '1\\n' ;;
                    *) exit 1 ;;
                  esac
                  exit 0
                fi
                printf 'qvm-features %s\\n' "$*" >> "$MUTATION_LOG"
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
            "qvm-create": """
                #!/usr/bin/env bash
                printf 'unexpected mutation: %s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 97
            """,
            "qvm-template-postprocess": """
                #!/usr/bin/env bash
                printf 'unexpected mutation: %s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 97
            """,
            "qvm-volume": """
                #!/usr/bin/env bash
                printf 'unexpected mutation: %s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 97
            """,
            "airlock-nix-domain-storage": """
                #!/usr/bin/env bash
                printf 'unexpected mutation: %s %s\\n' "$0" "$*" >> "$MUTATION_LOG"
                exit 97
            """,
            "qvm-service": """
                #!/usr/bin/env bash
                if [ "$#" -eq 2 ]; then
                  exit 1
                fi
                printf 'qvm-service %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qubes-prefs": """
                #!/usr/bin/env bash
                if [ "$#" -eq 1 ]; then
                  exit 1
                fi
                printf 'qubes-prefs %s\\n' "$*" >> "$MUTATION_LOG"
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
    assert setup["domains_changed"] == 0
    assert setup["root_images_changed"] == 0
    assert setup["store_volumes_changed"] == 0
    assert setup["store_views_changed"] == 0
    assert setup["restart_required"] is True
    assert setup["live_actions"] == []
    assert len(setup["restart_actions"]) == 3
    restart_reason = (
        "domain is running; stop it and re-run setup-config for this storage change "
        "to take effect"
    )
    for expected_type, expected_value in (
        ("domain-root", str(new_root_source)),
        ("domain-store", str(new_store_source)),
        ("domain-system", str(new_system_source)),
    ):
        assert any(
            action["type"] == expected_type
            and action["action"] == "restart"
            and action["domain"] == "sys-config-staging"
            and action["field"] == "source"
            and action["value"] == expected_value
            and action["state"] == "Running"
            and action["reason"] == restart_reason
            for action in setup["restart_actions"]
        )

    current_setup = json.loads((state_dir / "current-setup.json").read_text())
    assert current_setup["schema"] == "airlock.setup-config.v1"
    assert current_setup["status"] == "passed"
    assert current_setup["restart_required"] is True
    assert len(current_setup["restart_actions"]) == 3
    assert mutation_log.read_text(encoding="utf-8") == ""
    commands_log = (setup_log_dir / "setup-config.commands.log").read_text(
        encoding="utf-8"
    )
    assert "+ qvm-volume import sys-config-staging:root" not in commands_log
    assert (
        "+ airlock-nix-domain-storage ensure-store sys-config-staging"
        not in commands_log
    )
    assert "+ qvm-prefs sys-config-staging kernelopts" not in commands_log


def check_setup_config_nix_native(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    topology_path = work_dir / "topology.json"
    capabilities = work_dir / "xen-capabilities"
    plan_log_dir = work_dir / "plan-logs"
    apply_log_dir = work_dir / "apply-logs"
    policy_dir = work_dir / "policy.d"
    kernel_base = work_dir / "vm-kernels"
    system_source = work_dir / "system-source"
    root_source = work_dir / "root-source"
    store_source = work_dir / "store-source"
    root_image = root_source / "root.img"
    mutation_log = work_dir / "mutations.log"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        plan_log_dir,
        apply_log_dir,
        policy_dir,
        kernel_base,
        system_source,
        root_source,
        store_source,
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    _write_executable(
        system_source / "init",
        """
        #!/bin/sh
        exec /init
        """,
    )
    root_image.touch()
    (store_source / "store-paths").write_text(
        str(system_source) + "\n", encoding="utf-8"
    )
    (store_source / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "airlock.store-closure-manifest.v1",
                "name": "test-store",
                "read_only": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    mutation_log.touch()
    topology_path.write_text(
        json.dumps(
            {
                "schema": "airlock.qubes-topology.v1",
                "templates": {},
                "kernels": {},
                "domains": {
                    "legacy-fedora-app": {
                        "class": "AppVM",
                        "template": "fedora-44-xfce",
                        "label": "red",
                        "netvm": None,
                        "prefs": {"autostart": False},
                        "features": {
                            "airlock-managed": "1",
                            "airlock-role": "legacy-fedora-app",
                        },
                        "services": {},
                        "backend": {
                            "type": "qubes-template",
                            "template": "fedora-44-xfce",
                        },
                    },
                    "nix-native-work": {
                        "class": "StandaloneVM",
                        "template": None,
                        "label": "green",
                        "netvm": None,
                        "prefs": {
                            "autostart": False,
                            "include_in_backups": True,
                            "kernelopts": "console=hvc0 init=/sbin/init",
                            "memory": 1536,
                            "virt_mode": "pvh",
                        },
                        "features": {
                            "airlock-managed": "1",
                            "airlock-role": "nix-native-work",
                            "qrexec": "1",
                        },
                        "services": {},
                        "backend": {
                            "type": "nix-closure",
                            "template": None,
                            "system": {
                                "source": str(system_source),
                                "source_ref": None,
                            },
                            "kernel": {
                                "name": "6.12.64-1.fc41",
                                "source": None,
                                "source_ref": None,
                            },
                            "initrd": {
                                "name": None,
                                "source": None,
                                "source_ref": None,
                            },
                            "modules": {
                                "name": None,
                                "source": None,
                                "source_ref": None,
                            },
                            "root": {
                                "mode": "qubes-root-image",
                                "source": str(root_source),
                                "source_ref": None,
                                "read_only": False,
                            },
                            "store": {
                                "mode": "shared-closure",
                                "source": str(store_source),
                                "source_ref": None,
                                "view_backend": "erofs-objectmap",
                                "blob_device": "/dev/xvdf",
                                "mountpoint": "/nix/store",
                                "read_only": True,
                            },
                            "state": {
                                "persistent": "nix-native-work-private",
                                "persistent_size": 8589934592,
                                "volatile": None,
                            },
                        },
                    },
                },
                "prefs": {},
                "qrexec_policy": {"rules": []},
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
                exit 0
            """,
            "qvm-create": """
                #!/usr/bin/env bash
                printf 'qvm-create %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qvm-volume": """
                #!/usr/bin/env bash
                if [ "$1" = info ]; then
                  cat <<'INFO'
                pool               varlibqubes
                vid                appvms/nix-native-work/private
                rw                 True
                size               2147483648
                usage              0
                INFO
                  exit 0
                fi
                printf 'qvm-volume %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qvm-prefs": """
                #!/usr/bin/env bash
                if [ "$#" = 2 ]; then
                  exit 1
                fi
                printf 'qvm-prefs %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qvm-features": """
                #!/usr/bin/env bash
                if [ "$#" = 2 ]; then
                  exit 1
                fi
                printf 'qvm-features %s\\n' "$*" >> "$MUTATION_LOG"
            """,
            "qvm-service": """
                #!/usr/bin/env bash
                if [ "$#" = 2 ]; then
                  exit 1
                fi
                printf 'qvm-service %s\\n' "$*" >> "$MUTATION_LOG"
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
                printf 'unexpected command execution: %s\\n' "$0" >&2
                exit 97
            """,
            "qvm-template-postprocess": """
                #!/usr/bin/env bash
                printf 'unexpected command execution: %s\\n' "$0" >&2
                exit 97
            """,
            "qubesd-query": """
                #!/usr/bin/env bash
                printf 'unexpected command execution: %s\\n' "$0" >&2
                exit 97
            """,
            "airlock-nix-domain-storage": """
                #!/usr/bin/env bash
                printf 'airlock-nix-domain-storage %s\\n' "$*" >> "$MUTATION_LOG"
                cat <<JSON
                {"schema":"airlock.nix-domain-storage.v1","domain":"$2","volume":"store","mode":"shared-closure","source":"$4","read_only":true,"volume_created":true,"volumes_created":["store","store-data"],"view_created":true,"metadata_changed":true,"qubesd_restart_required":false,"qubesd_restarted":true,"generation":1,"closure_hash":"fixture","filesystem":"erofs","view_backend":"erofs-objectmap","view_image":"/var/lib/airlock/closure-store/domains/$2/generations/1/store.erofs","metadata_image":"/var/lib/airlock/closure-store/domains/$2/generations/1/store.erofs","blob_map":"/var/lib/airlock/closure-store/domains/$2/generations/1/store.blob-map.json","blob_volume":"store-data","blob_device":"/dev/xvdf","blob_logical_size":65536}
                JSON
            """,
        },
    )

    def run_setup(mode: str, output_dir: Path, state_dir: Path) -> None:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "MUTATION_LOG": str(mutation_log),
                "AIRLOCK_QUBES_KERNEL_BASE": str(kernel_base),
                "AIRLOCK_QUBES_POLICY_DIR": str(policy_dir),
                "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
                "AIRLOCK_LOG_DIR": str(output_dir),
                "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            }
        )
        _run(
            [
                "bash",
                str(repo_root / "lifecycle/misc/tools/scripts/setup-config.sh"),
                f"--{mode}",
                "--topology",
                str(topology_path),
            ],
            cwd=repo_root,
            env=env,
            stdout=output_dir / "stdout.log",
            stderr=output_dir / "stderr.log",
        )

    run_setup("plan", plan_log_dir, work_dir / "state-plan")
    plan = json.loads((plan_log_dir / "setup-config.json").read_text())
    assert plan["schema"] == "airlock.setup-config.v1"
    assert plan["status"] == "passed"
    assert plan["mode"] == "plan"
    assert plan["domains_changed"] == 2
    assert plan["root_images_changed"] == 1
    assert plan["store_volumes_changed"] == 2
    assert plan["state_volumes_changed"] == 1
    assert plan["store_views_changed"] == 1
    assert plan["unsupported_domains_count"] == 0
    plan_commands = (plan_log_dir / "setup-config.commands.log").read_text(
        encoding="utf-8"
    )
    expected_store_command = (
        f"+ airlock-nix-domain-storage ensure-store nix-native-work {store_source} "
        f"{store_source} --mode shared-closure --system {system_source} --root "
        f"{root_source} --kernel 6.12.64-1.fc41 --device /dev/xvde --blob-device "
        "/dev/xvdf --mountpoint /nix/store --view-backend erofs-objectmap"
    )
    for expected in (
        "+ qvm-create --class StandaloneVM --label green nix-native-work",
        "+ qvm-create --class AppVM --label red --template fedora-44-xfce "
        "legacy-fedora-app",
        f"+ qvm-volume import nix-native-work:root {root_image}",
        "+ qvm-volume resize nix-native-work:private 8589934592",
        expected_store_command,
    ):
        assert expected in plan_commands
    assert plan_commands.count("+ airlock-nix-domain-storage ensure-store ") == 1

    run_setup("apply", apply_log_dir, work_dir / "state-apply")
    apply = json.loads((apply_log_dir / "setup-config.json").read_text())
    assert apply["schema"] == "airlock.setup-config.v1"
    assert apply["status"] == "passed"
    assert apply["mode"] == "apply"
    assert apply["domains_changed"] == 2
    assert apply["root_images_changed"] == 1
    assert apply["store_volumes_changed"] == 2
    assert apply["state_volumes_changed"] == 1
    assert apply["store_views_changed"] == 1
    assert apply["qubesd_restarts"] == 1
    assert apply["unsupported_domains_count"] == 0
    live_actions = apply["live_actions"]
    assert any(
        action["type"] == "domain-root"
        and action["action"] == "import"
        and action["domain"] == "nix-native-work"
        for action in live_actions
    )
    assert any(
        action["type"] == "domain-state-volume"
        and action["action"] == "resize"
        and action["domain"] == "nix-native-work"
        and action["field"] == "private"
        and action["value"] == "8589934592"
        for action in live_actions
    )
    assert any(
        action["type"] == "domain-store-view"
        and action["action"] == "materialize"
        and action["domain"] == "nix-native-work"
        for action in live_actions
    )
    for volume in ("store", "store-data"):
        assert any(
            action["type"] == "domain-store-volume"
            and action["action"] == "create"
            and action["domain"] == "nix-native-work"
            and action["value"] == volume
            for action in live_actions
        )
    assert any(
        action["type"] == "systemd"
        and action["action"] == "restart"
        and action["field"] == "service"
        and action["value"] == "qubesd.service"
        for action in live_actions
    )
    mutation_lines = mutation_log.read_text(encoding="utf-8").splitlines()
    expected_mutations = [
        "qvm-create --class AppVM --label red --template fedora-44-xfce "
        "legacy-fedora-app",
        "qvm-create --class StandaloneVM --label green nix-native-work",
        f"qvm-volume import nix-native-work:root {root_image}",
        "qvm-volume resize nix-native-work:private 8589934592",
        "qvm-prefs nix-native-work kernel 6.12.64-1.fc41",
        f"qvm-prefs nix-native-work kernelopts console=hvc0 init=/sbin/init "
        f"airlock.system={system_source} airlock.storedev=/dev/xvde "
        "airlock.storefmt=erofs airlock.storeblobdev=/dev/xvdf",
        f"qvm-features nix-native-work airlock-root-source {root_source}",
        f"qvm-features nix-native-work airlock-store-source {store_source}",
        "qvm-features nix-native-work airlock-store-mode shared-closure",
        "qvm-features nix-native-work airlock-store-view-backend erofs-objectmap",
        "qvm-features nix-native-work airlock-store-filesystem erofs",
        "qvm-features nix-native-work airlock-store-generation 1",
        "qvm-features nix-native-work airlock-store-closure-hash fixture",
        "qvm-features nix-native-work airlock-store-view-image "
        "/var/lib/airlock/closure-store/domains/nix-native-work/generations/1/store.erofs",
        "qvm-features nix-native-work airlock-store-blob-map "
        "/var/lib/airlock/closure-store/domains/nix-native-work/generations/1/store.blob-map.json",
        "qvm-features nix-native-work airlock-store-blob-volume store-data",
        "qvm-features nix-native-work airlock-store-device /dev/xvde",
        "qvm-features nix-native-work airlock-store-blob-device /dev/xvdf",
        expected_store_command.removeprefix("+ "),
    ]
    for expected in expected_mutations:
        assert expected in mutation_lines
    assert (
        sum(
            line.startswith("airlock-nix-domain-storage ensure-store ")
            for line in mutation_lines
        )
        == 1
    )


    # A prepared USB already has this root installed. Its import source is
    # deliberately absent from dom0's store, so reconciliation must skip it.
    root_image.unlink()
    mutation_log.write_text("")
    features_command = bin_dir / "qvm-features"
    features_command.write_text(
        features_command.read_text().replace(
            'if [ "$#" = 2 ]; then',
            'if [ "$#" = 2 ] && [ "$2" = airlock-root-source ]; then\n'
            f"  printf '%s\\n' '{root_source}'\n  exit 0\nfi\n"
            'if [ "$#" = 2 ]; then',
        )
    )
    prepared_log = work_dir / "prepared-logs"
    prepared_log.mkdir()
    run_setup("apply", prepared_log, work_dir / "state-prepared")
    prepared_report = json.loads((prepared_log / "setup-config.json").read_text())
    assert prepared_report["root_images_changed"] == 0
    assert "qvm-volume import" not in mutation_log.read_text()


def check_setup_config_runtime_path(repo_root: Path, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    out = _run(
        [
            "nix",
            "build",
            f"{repo_root}#packages.x86_64-linux.airlock-setup-config",
            "--no-link",
            "--print-out-paths",
        ],
        cwd=repo_root,
    ).stdout.strip()
    wrapper = Path(_physical_store_path(repo_root, out)) / "bin/airlock-setup-config"
    wrapper_text = wrapper.read_text(encoding="utf-8")
    match = re.search(r'^export PATH="(.*):\$PATH"$', wrapper_text, re.MULTILINE)
    assert match is not None, f"could not extract runtime PATH from {wrapper}"
    qubesadmin_python_match = re.search(
        r"^export AIRLOCK_QUBESADMIN_PYTHON=\$\{AIRLOCK_QUBESADMIN_PYTHON:-([^}]+)\}$",
        wrapper_text,
        re.MULTILINE,
    )
    assert (
        qubesadmin_python_match is not None
    ), f"could not extract AIRLOCK_QUBESADMIN_PYTHON from {wrapper}"

    path_value = _physical_path_list(repo_root, match.group(1))
    qubesadmin_python = qubesadmin_python_match.group(1)
    required = [
        "airlock-gui-gpu-check",
        "awk",
        "chmod",
        "cmp",
        "cp",
        "dd",
        "grep",
        "ip",
        "jq",
        "mkdir",
        "mkfs.erofs",
        "mktemp",
        "dmsetup",
        "losetup",
        "qubes-prefs",
        "qubesd-query",
        "python3",
        "qvm-create",
        "qvm-features",
        "qvm-ls",
        "qvm-prefs",
        "qvm-service",
        "qvm-template-postprocess",
        "qvm-volume",
        "rm",
        "runuser",
        "sudo",
        "systemctl",
    ]
    resolved = {command: shutil.which(command, path=path_value) for command in required}
    (log_dir / "setup-config-runtime-path.commands").write_text(
        "".join(
            f"{command}: {path or '<missing>'}\n" for command, path in resolved.items()
        ),
        encoding="utf-8",
    )
    missing = [command for command, path in resolved.items() if path is None]
    assert not missing, "setup-config runtime PATH is missing helper(s): " + ", ".join(
        missing
    )
    _run_store_path(
        repo_root,
        qubesadmin_python,
        os.environ.copy(),
        ["-c", "import qubesadmin"],
    )
