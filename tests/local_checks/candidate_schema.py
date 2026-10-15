from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from .support import _nix_eval_json, _nix_eval_raw, _physical_store_path, _run, _write_flake


def check_config_candidate_nix_native(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    work_dir = log_dir / "work"
    shutil.rmtree(result_dir, ignore_errors=True)
    (work_dir / "airlock").mkdir(parents=True)

    initial_config = (
        repo_root / "lifecycle/bootstrap/initial-runtime-config"
    )
    shutil.copytree(initial_config / "dom0-profile", work_dir / "dom0-profile")
    for name in (
        "flake.nix",
        "airlock/config-vms.nix",
        "airlock/dom0-host.nix",
        "airlock/local.nix",
        "airlock/prebuilt-template-sources.nix",
        "airlock/system.nix",
    ):
        shutil.copy(initial_config / name, work_dir / name)
    shutil.copy(
        repo_root / "lifecycle/misc/lib/system.nix",
        work_dir / "airlock/system-schema.nix",
    )
    shutil.copy(
        repo_root / "lifecycle/misc/lib/topology.nix",
        work_dir / "airlock/topology.nix",
    )

    replacements = {
        "__AIRLOCK_NIXPKGS_URL__": "path:"
        + _physical_store_path(
            repo_root,
            _run(
                ["nix", "eval", "--raw", f"{repo_root}#airlock.configInputs.nixpkgs"]
            ).stdout.strip(),
        ),
        "__NIXPKGS_LIBVIRT_10_5_PATH__": _physical_store_path(
            repo_root,
            _run(
                [
                    "nix",
                    "eval",
                    "--raw",
                    f"{repo_root}#airlock.configInputs.nixpkgsLibvirt105",
                ]
            ).stdout.strip(),
        ),
        "__AIRLOCK_NIXOS_QUBES_PATH__": _physical_store_path(
            repo_root,
            _run(
                ["nix", "eval", "--raw", f"{repo_root}#airlock.configInputs.nixosQubes"]
            ).stdout.strip(),
        ),
        "__AIRLOCK_SUPPORT_PATH__": _physical_store_path(
            repo_root,
            _run(
                [
                    "nix",
                    "eval",
                    "--raw",
                    f"{repo_root}#airlock.configInputs.airlockSupport",
                ]
            ).stdout.strip(),
        ),
    }
    flake_path = work_dir / "flake.nix"
    flake_text = flake_path.read_text(encoding="utf-8")
    for needle, replacement in replacements.items():
        flake_text = flake_text.replace(needle, replacement)
    flake_path.write_text(flake_text, encoding="utf-8")

    _write_flake(
        work_dir / "airlock/runtime.nix",
        """
        { pkgs, ... }:
        {
          nixNative.domains.nix-native-generated = {
            label = "purple";
            netvm = null;
            hostName = "nix-native-generated";
            packages = with pkgs; [
              jq
            ];
            etc."airlock/nix-native-role".text = "generated\n";
            prefs.memory = 1280;
            features."airlock-role" = "nix-native-generated";
          };
        }
        """,
    )

    nix_flake_flags = [
        "nix",
        "--extra-experimental-features",
        "nix-command",
        "--extra-experimental-features",
        "flakes",
    ]
    _run([*nix_flake_flags, "flake", "lock", "--impure", "--offline"], cwd=work_dir)

    topology_json = result_dir / "topology.json"
    _run(
        [
            *nix_flake_flags,
            "eval",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--json",
            f"{work_dir}#airlock.topology",
        ],
        stdout=topology_json,
    )
    hostname = _run(
        [
            *nix_flake_flags,
            "eval",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--raw",
            f"{work_dir}#nixosConfigurations.airlock-domain-nix-native-generated.config.networking.hostName",
        ],
    ).stdout.strip()
    (result_dir / "hostname.txt").write_text(hostname + "\n", encoding="utf-8")
    etc_text = _run(
        [
            *nix_flake_flags,
            "eval",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--raw",
            f'{work_dir}#nixosConfigurations.airlock-domain-nix-native-generated.config.environment.etc."airlock/nix-native-role".text',
        ],
    ).stdout.strip()
    (result_dir / "etc.txt").write_text(etc_text + "\n", encoding="utf-8")

    runtime_source = _run(
        [
            *nix_flake_flags,
            "build",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            "--no-link",
            "--print-out-paths",
            "--expr",
            f'(builtins.getFlake "path:{work_dir}").nixosConfigurations.airlock-domain-nix-native-generated.config.environment.etc."qubes-rpc/airlock.NixStoreRuntimeCheck".source',
        ],
    ).stdout.strip()
    (result_dir / "runtime-check-source.txt").write_text(
        runtime_source + "\n", encoding="utf-8"
    )
    runtime_check = result_dir / "runtime-check.sh"
    shutil.copy(Path(_physical_store_path(repo_root, runtime_source)), runtime_check)
    runtime_text = runtime_check.read_text(encoding="utf-8")

    assert hostname == "nix-native-generated"
    assert etc_text == "generated"
    assert runtime_text.startswith("#!")
    for fragment in (
        "sed -n 's|^airlock.storedev=||p'",
        'mount_source="$(findmnt -rn -T /nix/store -o SOURCE)"',
        'case ",$mount_options," in',
        "*,ro,*)",
    ):
        assert fragment in runtime_text

    topology = json.loads(topology_json.read_text(encoding="utf-8"))
    domain = topology["domains"]["nix-native-generated"]
    assert domain["class"] == "StandaloneVM"
    assert domain["template"] is None
    assert domain["label"] == "purple"
    assert domain["netvm"] is None
    assert domain["prefs"]["memory"] == 1280
    assert domain["prefs"]["kernel"] == "6.12.64-1.fc41"
    assert "init=/sbin/init" in domain["prefs"]["kernelopts"]
    assert "airlock.system=/nix/store/" in domain["prefs"]["kernelopts"]
    assert "airlock.storedev=/dev/xvde" in domain["prefs"]["kernelopts"]
    assert domain["features"]["airlock-backend"] == "nix-closure"
    assert domain["features"]["airlock-role"] == "nix-native-generated"
    assert domain["backend"]["type"] == "nix-closure"
    assert (
        domain["backend"]["system"]["source_ref"]["attr"]
        == "nixosConfigurations.airlock-domain-nix-native-generated.config.system.build.toplevel"
    )
    assert domain["backend"]["root"]["mode"] == "qubes-root-image"
    assert (
        domain["backend"]["root"]["source_ref"]["attr"]
        == "packages.x86_64-linux.airlock-domain-nix-native-generated-root-image"
    )
    assert domain["backend"]["root"]["source_ref"]["subpath"] == "root.img"
    assert domain["backend"]["store"]["mode"] == "shared-closure"
    assert (
        domain["backend"]["store"]["source_ref"]["attr"]
        == "packages.x86_64-linux.airlock-domain-nix-native-generated-store-closure"
    )
    assert domain["backend"]["store"]["source_ref"]["subpath"] == ""
    assert domain["backend"]["store"]["mountpoint"] == "/nix/store"
    assert domain["backend"]["store"]["device"] == "/dev/xvde"
    assert domain["backend"]["store"]["read_only"] is True

    invalid_cases = [
        (
            """
            { ... }:
            {
              nixNative.domains.bad.pakages = [ ];
            }
            """,
            "airlock.nixNative.domains.bad: unknown option(s): pakages",
        ),
        (
            """
            { ... }:
            {
              nixNative.domains.bad.role = "sys-config-staging";
            }
            """,
            "airlock.nixNative.domains.bad.role: expected one of base, cmd, usr-assistant, sys-firewall, sys-gui, sys-net",
        ),
    ]
    invalid_log = result_dir / "invalid.log"
    for runtime_expr, expected in invalid_cases:
        _write_flake(work_dir / "airlock/runtime.nix", runtime_expr)
        result = _run(
            [
                *nix_flake_flags,
                "eval",
                "--impure",
                "--offline",
                "--no-write-lock-file",
                "--json",
                f"{work_dir}#airlock.topology",
            ],
            stdout=result_dir / "invalid.json",
            stderr=invalid_log,
            check=False,
        )
        assert result.returncode != 0
        assert expected in invalid_log.read_text(encoding="utf-8")


def check_config_topology_eval(repo_root: Path, log_dir: Path) -> None:
    """The active configuration topology belongs to the single controller."""
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = "nixosConfigurations.vm-gui-config-dev.config"
    topology = _nix_eval_json(repo_root, prefix + ".airlock.qubes.topology")
    assert not any(name.startswith("sys-config-") for name in topology["domains"])
    controller = topology["domains"]["sys-ctrl"]
    assert controller["class"] == "StandaloneVM"
    assert controller["netvm"] == "sys-firewall"
    assert controller["backend"]["type"] == "nix-closure"
    assert controller["backend"]["store"]["mode"] == "shared-closure"
    assert _nix_eval_json(repo_root, prefix + ".airlock.qubes.configAccessPolicy.enable") is False
    assert _nix_eval_json(repo_root, prefix + ".airlock.machineDeployment.enable") is True
    for service in ("MachinePlan", "MachineApply", "MachineControl"):
        assert _nix_eval_raw(repo_root, prefix + f'.environment.etc."qubes-rpc/airlock.Ctrl.{service}".mode') == "0555"
    (log_dir / "topology.json").write_text(json.dumps(topology, sort_keys=True) + "\n")


def check_config_candidate_flake(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    work_dir = result_dir / "work"
    shutil.rmtree(result_dir, ignore_errors=True)
    (work_dir / "airlock").mkdir(parents=True)
    for relative in (
        "dom0-profile",
        "flake.nix",
        "airlock/config-vms.nix",
        "airlock/dom0-host.nix",
        "airlock/local.nix",
        "airlock/runtime.nix",
        "airlock/prebuilt-template-sources.nix",
        "airlock/system.nix",
    ):
        source = (
            repo_root
            / "lifecycle/bootstrap/initial-runtime-config"
            / relative
        )
        target = work_dir / relative
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(
        repo_root / "lifecycle/misc/lib/system.nix",
        work_dir / "airlock/system-schema.nix",
    )
    shutil.copy2(
        repo_root / "lifecycle/misc/lib/topology.nix",
        work_dir / "airlock/topology.nix",
    )

    replacements = {
        "__AIRLOCK_NIXPKGS_URL__": "path:"
        + _physical_store_path(
            repo_root,
            _run(
                ["nix", "eval", "--raw", f"{repo_root}#airlock.configInputs.nixpkgs"]
            ).stdout.strip(),
        ),
        "__NIXPKGS_LIBVIRT_10_5_PATH__": _physical_store_path(
            repo_root,
            _run(
                [
                    "nix",
                    "eval",
                    "--raw",
                    f"{repo_root}#airlock.configInputs.nixpkgsLibvirt105",
                ]
            ).stdout.strip(),
        ),
        "__AIRLOCK_NIXOS_QUBES_PATH__": _physical_store_path(
            repo_root,
            _run(
                ["nix", "eval", "--raw", f"{repo_root}#airlock.configInputs.nixosQubes"]
            ).stdout.strip(),
        ),
        "__AIRLOCK_SUPPORT_PATH__": _physical_store_path(
            repo_root,
            _run(
                [
                    "nix",
                    "eval",
                    "--raw",
                    f"{repo_root}#airlock.configInputs.airlockSupport",
                ]
            ).stdout.strip(),
        ),
    }
    flake_path = work_dir / "flake.nix"
    flake_text = flake_path.read_text(encoding="utf-8")
    for needle, replacement in replacements.items():
        flake_text = flake_text.replace(needle, replacement)
    flake_path.write_text(flake_text, encoding="utf-8")

    nix_common = [
        "nix",
        "--extra-experimental-features",
        "nix-command",
        "--extra-experimental-features",
        "flakes",
    ]
    _run([*nix_common, "flake", "lock", "--impure", "--offline"], cwd=work_dir)
    _run(
        [
            *nix_common,
            "flake",
            "check",
            "--impure",
            "--offline",
            "--no-write-lock-file",
            str(work_dir),
            "--no-build",
            "--show-trace",
        ],
        cwd=work_dir,
    )

    def eval_work_json(attr: str) -> object:
        return json.loads(
            _run(
                [
                    *nix_common,
                    "eval",
                    "--impure",
                    "--offline",
                    "--no-write-lock-file",
                    "--json",
                    f"{work_dir}#{attr}",
                ],
                cwd=work_dir,
            ).stdout
        )

    def eval_work_raw(attr: str) -> str:
        return _run(
            [
                *nix_common,
                "eval",
                "--impure",
                "--offline",
                "--no-write-lock-file",
                "--raw",
                f"{work_dir}#{attr}",
            ],
            cwd=work_dir,
        ).stdout

    topology = eval_work_json("airlock.topology")
    system = eval_work_json("airlock.system")
    host_message = eval_work_raw(
        'nixosConfigurations.airlock-dom0.config.environment.etc."airlock/inside-config-applied-message".text'
    )
    host_derivations = _run(
        [
            *nix_common,
            "path-info",
            "--impure",
            "--offline",
            "--derivation",
            "--recursive",
            f"{work_dir}#nixosConfigurations.airlock-dom0.config.system.build.toplevel",
        ],
        cwd=work_dir,
    ).stdout
    profile_drv_path = eval_work_raw(
        "packages.x86_64-linux.airlock-dom0-profile.drvPath"
    )
    profile_derivations = json.loads(
        _run(
            [*nix_common, "derivation", "show", profile_drv_path],
            cwd=work_dir,
        ).stdout
    )["derivations"]
    profile_build_command = next(iter(profile_derivations.values()))["env"][
        "buildCommand"
    ]
    profile_source = work_dir / "dom0-profile"
    profile_json = json.loads(
        (profile_source / "share/airlock/profile.json").read_text(encoding="utf-8")
    )
    assert topology["schema"] == "airlock.qubes-topology.v1"
    assert topology["templates"] == {}
    assert topology["domains"]["sys-net"]["netvm"] is None
    assert topology["domains"]["sys-net"]["prefs"]["provides_network"] is True
    assert topology["domains"]["sys-net"]["prefs"]["virt_mode"] == "hvm"
    assert topology["domains"]["sys-firewall"]["netvm"] == "sys-net"
    assert topology["domains"]["sys-firewall"]["prefs"]["autostart"] is True
    assert topology["domains"]["sys-firewall"]["prefs"]["provides_network"] is True
    assert topology["domains"]["sys-firewall"]["features"]["servicevm"] == "1"
    assert not any(name.startswith("sys-config-") for name in topology["domains"])
    assert topology["domains"]["sys-ctrl"]["prefs"]["memory"] == 8192
    assert topology["domains"]["sys-ctrl"]["prefs"]["maxmem"] == 8192
    assert topology["prefs"]["default_netvm"] == "sys-firewall"
    assert "dev" not in topology["domains"]
    assert (
        topology["domains"]["sys-ctrl"]["features"][
            "airlock-config-applied-message"
        ]
        == "initial"
    )
    assert system["schema"] == "airlock.system.v1"
    assert system["dom0"]["profile"]["source"] == "dom0-profile"
    assert system["dom0"]["profile"]["activation_action"] == "run"
    assert system["dom0"]["profile"]["message"] == "initial"
    assert system["dom0"]["nixos"]["system"]["enable"] is True
    assert system["dom0"]["nixos"]["system"]["switch_action"] == "switch"
    assert system["plan"]["reboot_required"] is False
    assert system["plan"]["reboot_actions"] == []
    assert host_message.strip() == "initial"
    assert not re.search(
        r"airlock-nixos-(config-system|work)|qubes-template-airlock-nixos",
        host_derivations,
    )
    assert profile_json["schema"] == "airlock.dom0-profile.v1"
    assert profile_json["message"] == "initial"
    assert profile_json["built_by"] == "sys-ctrl"
    assert (profile_source / "bin/airlock-dom0-profile-activate").is_file()
    for expected in (
        'mkdir -p "$out/bin" "$out/share/airlock"',
        '"$out/share/airlock/profile.json"',
        '"$out/bin/airlock-dom0-profile-activate"',
        '"airlock.dom0-profile.v1"',
        '"sys-ctrl"',
    ):
        assert expected in profile_build_command

    runtime_path = work_dir / "airlock/runtime.nix"
    runtime_path.write_text(
        "{ ... }:\n{ dom0.host.modules = [ { } ]; }\n", encoding="utf-8"
    )
    module_system = eval_work_json("airlock.system")
    assert module_system["dom0"]["nixos"]["system"]["switch_action"] == "boot"
    assert module_system["plan"]["reboot_required"] is True
    negative_cases = [
        (
            "{ ... }:\n{ dom0.extraModules = [ ]; }\n",
            "airlock.system",
            "--json",
            "airlock.dom0: unknown option(s): extraModules",
        ),
        (
            '{ ... }:\n{ configVms.guests.sys-ctrl.hostname = "old-name"; }\n',
            "airlock.configRoleConfigurations.sys-ctrl.config.networking.hostName",
            "--raw",
            "airlock.configVms.guests.sys-ctrl: unknown option(s): hostname",
        ),
        (
            '{ ... }:\n{ nixNative.domains.work = { role = "base"; hostname = "old-name"; }; }\n',
            "airlock.topology",
            "--json",
            "airlock.nixNative.domains.work: unknown option(s): hostname",
        ),
        (
            "{ ... }:\n{\n  templates.definitions.airlock-nixos-bad = { };\n}\n",
            "airlock.topology",
            "--json",
            "airlock.runtime: unknown option(s): templates",
        ),
        (
            "{ ... }:\n{\n  configVms.guests.config-stagin = { };\n}\n",
            "airlock.configRoleConfigurations.sys-ctrl.config.networking.hostName",
            "--raw",
            "airlock.configVms.guests: unknown role(s): config-stagin",
        ),
        (
            '{ ... }:\n{\n  configVms.domains.sys-ctrl.template = "old-template";\n}\n',
            "airlock.topology",
            "--json",
            "airlock.configVms.domains.sys-ctrl: unknown option(s): template",
        ),
        (
            '{ ... }:\n{\n  dom0.host.apply.mode = "live";\n}\n',
            "airlock.system",
            "--json",
            "airlock.dom0.host.apply.mode: expected one of auto, switch, boot",
        ),
        (
            "{ ... }:\n{\n  dom0.host.apply.reason = true;\n}\n",
            "airlock.system",
            "--json",
            "airlock.dom0.host.apply.reason: expected a string or path, got bool",
        ),
        (
            "{ ... }:\n{\n  dom0.host.pakages = [ ];\n}\n",
            "airlock.system",
            "--json",
            "airlock.dom0.host: unknown option(s): pakages",
        ),
        (
            "{ ... }:\n{\n  unknowable = true;\n}\n",
            "airlock.topology",
            "--json",
            "airlock.runtime: unknown option(s): unknowable",
        ),
    ]
    for index, (runtime_text, attr, mode, expected_error) in enumerate(negative_cases):
        runtime_path.write_text(runtime_text, encoding="utf-8")
        result = _run(
            [
                *nix_common,
                "eval",
                "--impure",
                "--offline",
                "--no-write-lock-file",
                mode,
                f"{work_dir}#{attr}",
            ],
            cwd=work_dir,
            stderr=result_dir / f"invalid-{index}.log",
            check=False,
        )
        assert result.returncode != 0
        assert expected_error in (result_dir / f"invalid-{index}.log").read_text(
            encoding="utf-8"
        )
