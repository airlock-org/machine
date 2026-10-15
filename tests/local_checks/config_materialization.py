from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from pathlib import Path
from .support import _git_commit_all, _run, _setup_fake_command_dir, _write_executable, _write_flake


def check_config_apply_template_build_ref(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    candidate_dir = work_dir / "candidate"
    apply_log_dir = work_dir / "logs"
    capabilities = work_dir / "xen-capabilities"
    validated_tar = work_dir / "config-candidate-validated.tar"
    setup_capture = work_dir / "setup-topology.json"
    state_dir = work_dir / "state"
    etc_dir = work_dir / "etc"
    system_profile = work_dir / "airlock-system-profile"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (bin_dir, candidate_dir, apply_log_dir):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")
    nixpkgs_path = _run(
        ["nix", "eval", "--raw", f"{repo_root}#airlock.configInputs.nixpkgs"]
    ).stdout.strip()

    _write_flake(
        candidate_dir / "flake.nix",
        f"""
        {{
          description = "Airlock config apply template build-ref fixture";

          inputs.nixpkgs.url = "path:{nixpkgs_path}";

          outputs =
            {{ self, nixpkgs }}:
            let
              system = "x86_64-linux";
              pkgs = nixpkgs.legacyPackages.${{system}};
            in
            {{
              packages.${{system}}.test-template =
                pkgs.runCommand "test-template"
                  {{
                    preferLocalBuild = true;
                    allowSubstitutes = false;
                  }}
                  ''
                    mkdir -p "$out/test-template"
                    printf 'root image fixture\n' > "$out/test-template/root.img"
                    printf 'qrexec=1\nvirt-mode=pvh\n' > "$out/test-template/template.conf"
                  '';

              airlock.topology = {{
                schema = "airlock.qubes-topology.v1";
                templates.test-template = {{
                  import = true;
                  source = null;
                  source_ref = {{
                    type = "flake-output";
                    attr = "packages.x86_64-linux.test-template";
                    subpath = "test-template";
                  }};
                  skip_start = true;
                }};
                kernels = {{ }};
                domains = {{ }};
                prefs = {{ }};
                qrexec_policy.rules = [ ];
              }};

              airlock.system = {{
                schema = "airlock.system.v1";
                dom0 = {{
                  profile = {{
                    source = null;
                    source_ref = null;
                    activation_command = "bin/airlock-dom0-profile-activate";
                    activation_action = "none";
                    message = "";
                  }};
                  packages = [ ];
                  systemd.services = {{ }};
                  nixos = {{
                    system = {{
                      enable = false;
                      reason = "dom0 system change requires reboot";
                      source = null;
                      source_ref = null;
                    }};
                    kernel = {{
                      enable = false;
                      reason = "dom0 kernel change requires reboot";
                      source = null;
                      source_ref = null;
                    }};
                    xen = {{
                      enable = false;
                      reason = "dom0 xen change requires reboot";
                      source = null;
                      source_ref = null;
                    }};
                    bootloader = {{
                      enable = false;
                      reason = "dom0 bootloader change requires reboot";
                      source = null;
                      source_ref = null;
                    }};
                    qubes = {{
                      enable = false;
                      reason = "dom0 qubes change requires reboot";
                      source = null;
                      source_ref = null;
                    }};
                  }};
                }};
                plan = {{
                  live_actions = [ ];
                  reboot_actions = [ ];
                  reboot_required = false;
                  reboot_reasons = [ ];
                }};
              }};
            }};
        }}
        """,
    )
    nix_flags = [
        "nix",
        "--extra-experimental-features",
        "nix-command",
        "--extra-experimental-features",
        "flakes",
    ]
    _run([*nix_flags, "flake", "lock", "--impure", "--offline"], cwd=candidate_dir)
    _git_commit_all(
        candidate_dir,
        "fixture candidate",
        name="Airlock Test",
        email="airlock-test@localhost",
    )
    candidate_commit = _run(
        ["git", "rev-parse", "HEAD"], cwd=candidate_dir
    ).stdout.strip()
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
    (work_dir / "validation.json").write_text(
        json.dumps(
            {
                "schema": "airlock.config-validation.v1",
                "status": "passed",
                "commit": candidate_commit,
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
                "commit": candidate_commit,
                "branch": "main",
                "source_role": "sys-config-staging",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _run(
        [
            *nix_flags,
            "eval",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--json",
            f"{candidate_dir}#airlock.topology",
        ],
        stdout=work_dir / "topology.json",
    )
    _run(
        [
            *nix_flags,
            "eval",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--json",
            f"{candidate_dir}#airlock.system",
        ],
        stdout=work_dir / "system.json",
    )
    with tarfile.open(validated_tar, "w") as tar:
        for name in (
            "config.bundle",
            "validation.json",
            "manifest.json",
            "topology.json",
            "system.json",
        ):
            tar.add(work_dir / name, arcname=name)

    _write_executable(
        bin_dir / "airlock-config-archive-candidate",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        cp "$VALIDATED_TAR_FIXTURE" "$AIRLOCK_LOG_DIR/config-candidate-validated.tar"
        jq -c -n \
          --arg schema "airlock.config-archive.v1" \
          --arg status "passed" \
          --arg candidate_commit "$CANDIDATE_COMMIT_FIXTURE" \
          '{
            schema: $schema,
            status: $status,
            candidate_commit: $candidate_commit,
            validation_commit: $candidate_commit,
            validation: {
              schema: "airlock.config-validation.v1",
              status: "passed",
              commit: $candidate_commit
            },
            vault_archive: {
              schema: "airlock.config-vault-archive.v1",
              status: "passed",
              candidate_commit: $candidate_commit
            }
          }' > "$AIRLOCK_LOG_DIR/config-archive.json"
        printf 'archive fixture ok\n'
        """,
    )
    _write_executable(
        bin_dir / "airlock-setup-config",
        """
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
        cp "$topology" "$SETUP_CAPTURE_FIXTURE"
        jq -e '
          .templates."test-template".source != null and
          .templates."test-template".source_ref == null and
          (.templates."test-template".source | test("^/nix/store/")) and
          (.templates."test-template".source | test("/test-template$"))
        ' "$topology" >/dev/null
        jq -c -n \
          --arg schema "airlock.setup-config.v1" \
          --arg status "passed" \
          '{schema: $schema, status: $status, domains_changed: 0, templates_changed: 1}' \
          > "$AIRLOCK_LOG_DIR/setup-config.json"
        printf 'setup fixture ok\n'
        """,
    )
    _write_executable(
        bin_dir / "qrexec-client",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        cat >/dev/null
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
    )
    _write_executable(
        bin_dir / "systemctl",
        """
        #!/usr/bin/env bash
        if [ "$1" = "is-active" ]; then
          exit 0
        fi
        exit 1
        """,
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CANDIDATE_COMMIT_FIXTURE": candidate_commit,
            "SETUP_CAPTURE_FIXTURE": str(setup_capture),
            "VALIDATED_TAR_FIXTURE": str(validated_tar),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(apply_log_dir),
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_SYSTEM_ETC_DIR": str(etc_dir),
            "AIRLOCK_SYSTEM_PROFILE": str(system_profile),
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
    result = json.loads(
        (apply_log_dir / "config-apply.json").read_text(encoding="utf-8")
    )
    assert result["schema"] == "airlock.config-apply.v1"
    assert result["status"] == "passed"
    assert result["candidate_commit"] == candidate_commit
    assert result["setup"]["status"] == "passed"
    assert result["deployment"]["status"] == "passed"
    template_builds = json.loads(
        (apply_log_dir / "config-apply-template-builds.json").read_text(
            encoding="utf-8"
        )
    )
    assert template_builds["schema"] == "airlock.template-builds.v1"
    assert len(template_builds["builds"]) == 1
    build = template_builds["builds"][0]
    assert build["template"] == "test-template"
    assert build["attr"] == "packages.x86_64-linux.test-template"
    assert build["status"] == "passed"
    assert re.search(r"^/nix/store/.*/test-template$", build["source_path"])
    system_builds = json.loads(
        (apply_log_dir / "config-apply-system-builds.json").read_text(encoding="utf-8")
    )
    assert system_builds["schema"] == "airlock.system-builds.v1"
    assert system_builds["builds"] == []
    system_apply = json.loads(
        (apply_log_dir / "config-system-apply.json").read_text(encoding="utf-8")
    )
    assert system_apply["schema"] == "airlock.system-apply.v1"
    assert system_apply["status"] == "passed"
    assert system_apply["profile_status"] == "skipped"
    assert system_apply["reboot_required"] is False
    captured_topology = json.loads(setup_capture.read_text(encoding="utf-8"))
    assert captured_topology["templates"]["test-template"]["source"] is not None
    assert captured_topology["templates"]["test-template"]["source_ref"] is None


def check_config_apply_nix_domain_materialization(
    repo_root: Path, log_dir: Path
) -> None:
    work_dir = log_dir
    bin_dir = work_dir / "bin"
    candidate_dir = work_dir / "candidate"
    apply_log_dir = work_dir / "logs"
    capabilities = work_dir / "xen-capabilities"
    state_dir = work_dir / "state"
    etc_dir = work_dir / "etc"
    validated_tar = work_dir / "config-candidate-validated.tar"
    deployment_tar_capture = work_dir / "deployment.tar"
    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        candidate_dir / "system-source",
        candidate_dir / "root-source",
        candidate_dir / "store-source",
        apply_log_dir,
        state_dir,
        etc_dir,
    ):
        path.mkdir(parents=True)
    capabilities.write_text("control_d\n", encoding="utf-8")

    _write_flake(
        candidate_dir / "flake.nix",
        """
        {
          description = "Airlock Nix-native domain materialization fixture";
          outputs = { self }: { };
        }
        """,
    )
    system_source = candidate_dir / "system-source"
    root_source = candidate_dir / "root-source"
    store_source = candidate_dir / "store-source"
    _write_executable(
        system_source / "init",
        """
        #!/bin/sh
        exec /init
        """,
    )
    (root_source / "root.img").touch()
    (store_source / "store-paths").write_text(
        str(system_source) + "\n", encoding="utf-8"
    )
    (store_source / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "airlock.store-closure-manifest.v1",
                "name": "fixture-store",
                "store_paths_file": "store-paths",
                "read_only": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
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
    topology = {
        "schema": "airlock.qubes-topology.v1",
        "templates": {},
        "kernels": {},
        "domains": {
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
                    "system": {"source": "system-source", "source_ref": None},
                    "kernel": {
                        "name": "6.12.64-1.fc41",
                        "source": None,
                        "source_ref": None,
                    },
                    "initrd": {"name": None, "source": None, "source_ref": None},
                    "modules": {"name": None, "source": None, "source_ref": None},
                    "root": {
                        "mode": "qubes-root-image",
                        "source": "root-source",
                        "source_ref": None,
                        "read_only": False,
                    },
                    "store": {
                        "mode": "shared-closure",
                        "source": "store-source",
                        "source_ref": None,
                        "mountpoint": "/nix/store",
                        "device": "/dev/xvde",
                        "blob_device": "/dev/xvdf",
                        "view_backend": "erofs-objectmap",
                        "read_only": True,
                    },
                    "state": {
                        "persistent": "nix-native-work-private",
                        "volatile": None,
                    },
                },
            }
        },
        "prefs": {},
        "qrexec_policy": {"rules": []},
    }
    (work_dir / "topology.json").write_text(
        json.dumps(topology, sort_keys=True) + "\n", encoding="utf-8"
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
            "airlock-setup-config": """
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
                [ -n "$topology" ]
                jq -e \
                  --arg state "$SYSTEM_STATE_DIR_FIXTURE" \
                  --arg commit "$CANDIDATE_COMMIT_FIXTURE" \
                  '
                    .domains."nix-native-work".backend.type == "nix-closure" and
                    .domains."nix-native-work".backend.system.source_ref == null and
                    .domains."nix-native-work".backend.root.source_ref == null and
                    .domains."nix-native-work".backend.store.source_ref == null and
                    (.domains."nix-native-work".backend.system.source | startswith($state + "/sources/" + $commit + "/nix-domain-nix-native-work-system")) and
                    (.domains."nix-native-work".backend.root.source | startswith($state + "/sources/" + $commit + "/nix-domain-nix-native-work-root")) and
                    (.domains."nix-native-work".backend.store.source | startswith($state + "/sources/" + $commit + "/nix-domain-nix-native-work-store"))
                  ' "$topology" >/dev/null
                jq -c -n \
                  --arg schema "airlock.setup-config.v1" \
                  '{
                    schema: $schema,
                    status: "passed",
                    mode: "apply",
                    reason: "fixture setup passed",
                    templates_changed: 0,
                    kernels_changed: 0,
                    domains_changed: 1,
                    prefs_changed: 0,
                    features_changed: 0,
                    services_changed: 0,
                    qrexec_policy_changed: 0,
                    root_images_changed: 1,
                    live_actions: [],
                    restart_actions: [],
                    unsupported_domains: [],
                    unsupported_domains_count: 0,
                    restart_required: false
                  }' > "$AIRLOCK_LOG_DIR/setup-config.json"
                cat "$AIRLOCK_LOG_DIR/setup-config.json"
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
            "CANDIDATE_COMMIT_FIXTURE": commit,
            "DEPLOYMENT_TAR_CAPTURE_FIXTURE": str(deployment_tar_capture),
            "SYSTEM_STATE_DIR_FIXTURE": str(state_dir),
            # RPC commands are mocked; never wait for the live VM's sockets.
            "AIRLOCK_QREXEC_RUN_DIR": str(work_dir / "no-qrexec-runtime"),
            "AIRLOCK_XEN_CAPABILITIES": str(capabilities),
            "AIRLOCK_LOG_DIR": str(apply_log_dir),
            "AIRLOCK_USE_HOST_NIX_STORE": "1",
            "AIRLOCK_SYSTEM_STATE_DIR": str(state_dir),
            "AIRLOCK_SYSTEM_ETC_DIR": str(etc_dir),
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
    assert apply_payload["reason"] == "validated config candidate applied"
    assert apply_payload["setup"]["status"] == "passed"
    assert apply_payload["system_apply_result"]["status"] == "passed"
    assert apply_payload["nix_domain_builds_result"]["schema"] == (
        "airlock.nix-domain-builds.v1"
    )
    materializations = apply_payload["nix_domain_materializations_result"]
    assert materializations["schema"] == "airlock.nix-domain-materializations.v1"
    assert materializations["domains"][0]["domain"] == "nix-native-work"
    assert materializations["domains"][0]["root"]["source"].startswith(
        f"{state_dir}/sources/{commit}/nix-domain-nix-native-work-root"
    )
    assert apply_payload["unsupported_domain_backends_result"]["schema"] == (
        "airlock.unsupported-domain-backends.v1"
    )
    assert apply_payload["unsupported_domain_backends_result"]["domains"] == []
    assert apply_payload["deployment"]["status"] == "passed"
    assert (
        apply_payload["deployment"]["nix_domain_materializations"]["domains"][0][
            "domain"
        ]
        == "nix-native-work"
    )
    assert apply_payload["deployment_archive"]["status"] == "passed"

    current_deployment = json.loads((state_dir / "current-deployment.json").read_text())
    assert current_deployment["schema"] == "airlock.config-deployment.v1"
    assert current_deployment["status"] == "passed"
    assert current_deployment["candidate_commit"] == commit
    assert (
        current_deployment["nix_domain_builds"]["schema"]
        == "airlock.nix-domain-builds.v1"
    )
    assert current_deployment["nix_domain_materializations"]["schema"] == (
        "airlock.nix-domain-materializations.v1"
    )

    with tarfile.open(deployment_tar_capture) as tar:
        members = sorted(tar.getnames())
        assert "nix-domain-builds.json" in members
        assert "nix-domain-materializations.json" in members
        assert "unsupported-domain-backends.json" in members
        assert "logs/nix-domain-builds.log" in members
        nix_domain_builds = json.load(tar.extractfile("nix-domain-builds.json"))
        extracted_materializations = json.load(
            tar.extractfile("nix-domain-materializations.json")
        )
        unsupported = json.load(tar.extractfile("unsupported-domain-backends.json"))
        deployment = json.load(tar.extractfile("deployment.json"))

    assert nix_domain_builds["schema"] == "airlock.nix-domain-builds.v1"
    assert (
        extracted_materializations["schema"] == "airlock.nix-domain-materializations.v1"
    )
    assert extracted_materializations["domains"][0]["domain"] == "nix-native-work"
    assert extracted_materializations["domains"][0]["root"]["mode"] == (
        "qubes-root-image"
    )
    assert extracted_materializations["domains"][0]["store"]["mode"] == (
        "shared-closure"
    )
    assert extracted_materializations["domains"][0]["store"]["view_backend"] == (
        "erofs-objectmap"
    )
    assert unsupported["schema"] == "airlock.unsupported-domain-backends.v1"
    assert unsupported["domains"] == []
    assert deployment["artifacts"]["nix_domain_builds"] == "nix-domain-builds.json"
    assert (
        deployment["artifacts"]["nix_domain_materializations"]
        == "nix-domain-materializations.json"
    )
    assert (
        deployment["artifacts"]["unsupported_domain_backends"]
        == "unsupported-domain-backends.json"
    )
