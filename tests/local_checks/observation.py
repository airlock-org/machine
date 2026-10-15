from __future__ import annotations

import json
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from .support import _command_available, _command_output, _file_contains_word, _first_cpu_flags, _first_readable, _run, _write_executable


def check_host_capabilities(repo_root: Path, log_dir: Path) -> None:
    del repo_root
    output = log_dir / "host-capabilities.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    xen_capabilities = Path("/proc/xen/capabilities")
    cpu_flags = _first_cpu_flags()
    report = {
        "schema": "airlock.host-capabilities.v1",
        "host": {
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "virtualization": _command_output(["systemd-detect-virt"]),
        },
        "nix": {
            "available": _command_available("nix"),
            "version": _command_output(["nix", "--version"]),
        },
        "xen": {
            "proc_xen": Path("/proc/xen").exists(),
            "control_domain": _file_contains_word(xen_capabilities, "control_d"),
            "capabilities": (
                xen_capabilities.read_text(encoding="utf-8", errors="replace").strip()
                if xen_capabilities.is_file()
                else ""
            ),
        },
        "hardware_virtualization": {
            "vmx": "vmx" in cpu_flags,
            "svm": "svm" in cpu_flags,
            "ept": "ept" in cpu_flags,
            "npt": "npt" in cpu_flags,
            "iommu_groups": any(Path("/sys/kernel/iommu_groups").glob("*")),
            "kvm_nested": _first_readable(
                [
                    Path("/sys/module/kvm_intel/parameters/nested"),
                    Path("/sys/module/kvm_amd/parameters/nested"),
                ]
            ),
        },
        "qubes_tools": {
            "qvm_run": _command_available("qvm-run"),
            "qvm_create": _command_available("qvm-create"),
            "qubes_prefs": _command_available("qubes-prefs"),
        },
    }
    output.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    assert report["schema"] == "airlock.host-capabilities.v1"


def check_gc_airlock_store(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir
    data_root = work_dir / "data"
    nix_root = data_root / "nix-root"
    store_dir = nix_root / "nix/store"
    vm_dir = data_root / "vm"
    gc_log_dir = data_root / "logs"
    result_link_dir = data_root / "result-links"
    bin_dir = work_dir / "bin"
    mock_log = work_dir / "mock.log"

    shutil.rmtree(work_dir, ignore_errors=True)
    for path in (
        bin_dir,
        store_dir / "11111111111111111111111111111111-vm-image",
        store_dir / "22222222222222222222222222222222-backing-image",
        store_dir / "33333333333333333333333333333333-system",
        store_dir / "44444444444444444444444444444444-tool",
        vm_dir,
        gc_log_dir,
        result_link_dir,
    ):
        path.mkdir(parents=True)
    (vm_dir / "airlock-vm-gui-config-dev-image-result").symlink_to(
        store_dir / "11111111111111111111111111111111-vm-image"
    )
    (result_link_dir / "qemu-kvm-result").symlink_to(
        "/nix/store/44444444444444444444444444444444-tool"
    )
    (vm_dir / "airlock-vm-gui-config-dev-image.qcow2").touch()
    (gc_log_dir / "config-apply.json").write_text(
        json.dumps(
            {
                "schema": "airlock.config-apply.v1",
                "system_apply_result": {
                    "host_system_source": "/nix/store/33333333333333333333333333333333-system"
                },
                "apply_log": (
                    "this noisy log may mention "
                    "/nix/store/99999999999999999999999999999999-do-not-root"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_executable(
        bin_dir / "nix-store",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        {
          printf 'NIX_CONFIG=%s\n' "${NIX_CONFIG:-}"
          printf 'ARGS='
          printf '[%s]' "$@"
          printf '\n'
        } >> "$AIRLOCK_GC_TEST_MOCK_LOG"
        case "$*" in
          "--gc --print-dead")
            printf '/nix/store/deadbeefdeadbeefdeadbeefdeadbeef-dead\n'
            printf 'removing stale temporary roots file fixture\n' >&2
            ;;
          "--gc")
            printf 'freed 0 bytes\n'
            ;;
          *)
            printf 'unexpected nix-store args: %s\n' "$*" >&2
            exit 2
            ;;
        esac
        """,
    )
    _write_executable(
        bin_dir / "qemu-img",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "$1" = "info" ]; then
          printf '%s\n' 'image: test.qcow2'
          printf '%s\n' 'file format: qcow2'
          printf '%s\n' 'backing file: /nix/store/22222222222222222222222222222222-backing-image/base.qcow2'
          exit 0
        fi
        printf 'unexpected qemu-img args: %s\n' "$*" >&2
        exit 2
        """,
    )

    base_env = os.environ.copy()
    base_env.update(
        {
            "PATH": f"{bin_dir}:{base_env['PATH']}",
            "AIRLOCK_GC_ALLOW_NONSTANDARD_DATA_ROOT": "1",
            "AIRLOCK_GC_TEST_MOCK_LOG": str(mock_log),
            "AIRLOCK_DATA_ROOT": str(data_root),
            "AIRLOCK_NIX_ROOT": str(nix_root),
            "AIRLOCK_NIX_STORE": str(nix_root),
            "AIRLOCK_VM_DIR": str(vm_dir),
            "AIRLOCK_LOG_DIR": str(gc_log_dir),
            "AIRLOCK_RESULT_LINK_DIR": str(result_link_dir),
            "AIRLOCK_NIX_STORE_BIN": str(bin_dir / "nix-store"),
            "AIRLOCK_QEMU_IMG_BIN": str(bin_dir / "qemu-img"),
        }
    )

    _run(
        [
            str(repo_root / "lifecycle/misc/tools/scripts/gc-airlock-store.sh"),
            "dry-run",
        ],
        cwd=repo_root,
        env=base_env,
        stdout=work_dir / "dry-run.out",
    )
    roots_dir = nix_root / "nix/var/nix/gcroots/airlock"
    manifest_text = (roots_dir / "manifest.tsv").read_text(encoding="utf-8")
    mock_text = mock_log.read_text(encoding="utf-8")
    dry_run_text = (work_dir / "dry-run.out").read_text(encoding="utf-8")
    assert f"store = {nix_root}" in mock_text
    assert "ARGS=[--gc][--print-dead]" in mock_text
    for store_name in (
        "11111111111111111111111111111111-vm-image",
        "22222222222222222222222222222222-backing-image",
        "33333333333333333333333333333333-system",
        "44444444444444444444444444444444-tool",
    ):
        assert f"/nix/store/{store_name}" in manifest_text
    assert "99999999999999999999999999999999-do-not-root" not in manifest_text
    assert "Dead path count:  1" in dry_run_text
    assert "Dead stderr log:" in dry_run_text
    assert "Result JSON:" in dry_run_text
    assert "removing stale temporary roots file fixture" in (
        gc_log_dir / "airlock-store-gc-dead-paths.stderr.log"
    ).read_text(encoding="utf-8")
    gc_json = json.loads(
        (gc_log_dir / "airlock-store-gc.json").read_text(encoding="utf-8")
    )
    assert gc_json["schema"] == "airlock.store-gc.v1"
    assert gc_json["mode"] == "dry-run"
    assert gc_json["nix_root"] == str(nix_root)
    assert gc_json["root_count"] == 4
    assert gc_json["dead_count"] == 1
    assert gc_json["gc_ran"] is False
    assert gc_json["dead_stderr_log"] == str(
        gc_log_dir / "airlock-store-gc-dead-paths.stderr.log"
    )

    _run(
        [str(repo_root / "lifecycle/misc/tools/scripts/gc-airlock-store.sh"), "run"],
        cwd=repo_root,
        env=base_env,
        stdout=work_dir / "run.out",
    )
    run_text = (work_dir / "run.out").read_text(encoding="utf-8")
    assert "ARGS=[--gc]" in mock_log.read_text(encoding="utf-8")
    assert "GC output tail:" in run_text
    assert "freed 0 bytes" in run_text
    gc_json = json.loads(
        (gc_log_dir / "airlock-store-gc.json").read_text(encoding="utf-8")
    )
    assert gc_json["schema"] == "airlock.store-gc.v1"
    assert gc_json["mode"] == "run"
    assert gc_json["gc_ran"] is True

    auto_env = base_env | {"AIRLOCK_GC_AUTO_THRESHOLD_GIB": "0"}
    _run(
        [str(repo_root / "lifecycle/misc/tools/scripts/gc-airlock-store.sh"), "auto"],
        cwd=repo_root,
        env=auto_env,
        stdout=work_dir / "auto.out",
    )
    auto_text = (work_dir / "auto.out").read_text(encoding="utf-8")
    mock_lines = mock_log.read_text(encoding="utf-8").splitlines()
    assert "Auto decision:    running full GC" in auto_text
    assert mock_lines.count("ARGS=[--gc][--print-dead]") == 2
    assert mock_lines.count("ARGS=[--gc]") == 2
    gc_json = json.loads(
        (gc_log_dir / "airlock-store-gc.json").read_text(encoding="utf-8")
    )
    assert gc_json["schema"] == "airlock.store-gc.v1"
    assert gc_json["mode"] == "auto"
    assert gc_json["gc_ran"] is True
    assert gc_json["dead_count"] == 1

    nonstandard_root = Path(f"/tmp/airlock-gc-nonstandard-{os.getpid()}")
    result = _run(
        [
            str(repo_root / "lifecycle/misc/tools/scripts/gc-airlock-store.sh"),
            "dry-run",
        ],
        cwd=repo_root,
        env={
            **os.environ,
            "AIRLOCK_DATA_ROOT": str(nonstandard_root),
            "AIRLOCK_NIX_ROOT": str(nonstandard_root / "nix-root"),
            "AIRLOCK_NIX_STORE": str(nonstandard_root / "nix-root"),
            "AIRLOCK_LOG_DIR": str(nonstandard_root / "logs"),
            "AIRLOCK_VM_DIR": str(nonstandard_root / "vm"),
        },
        stdout=work_dir / "nonstandard.out",
        stderr=work_dir / "nonstandard.out",
        check=False,
    )
    assert result.returncode != 0
    assert "refusing nonstandard data root" in (work_dir / "nonstandard.out").read_text(
        encoding="utf-8"
    )


def check_config_repo_status(repo_root: Path, log_dir: Path) -> None:
    script = (
        repo_root / "lifecycle/misc/tools/scripts/nixos-config-vms-smoke.sh"
    ).read_text(encoding="utf-8")
    helper = re.search(
        r"^repo_status_is_clean_main\(\) \{\n.*?^\}", script, re.M | re.S
    )
    assert helper is not None
    log_dir.mkdir(parents=True, exist_ok=True)
    # Exercise the production predicate against real Git output, including the
    # bundle-clone tracking state that triggered the VM preflight failure.
    with tempfile.TemporaryDirectory(prefix="airlock-repo-status-") as tmp:
        root = Path(tmp)
        source = root / "source"
        staging = root / "staging"

        def git(repo: Path, *args: str) -> str:
            return _run(
                [
                    "git",
                    "-c",
                    "user.name=Airlock Test",
                    "-c",
                    "user.email=airlock-test@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(repo),
                    *args,
                ]
            ).stdout

        def assert_status(repo: Path, name: str, expected: bool) -> None:
            output = log_dir / f"{name}.status"
            output.write_text(
                git(
                    repo,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                ),
                encoding="utf-8",
            )
            result = _run(
                [
                    "bash",
                    "-c",
                    helper.group(0) + '\nrepo_status_is_clean_main "$1"',
                    "bash",
                    str(output),
                ],
                check=False,
            )
            assert (result.returncode == 0) == expected, name

        source.mkdir()
        git(source, "init", "-b", "main")
        assert_status(source, "unborn", False)
        tracked = source / "tracked"
        tracked.write_text("baseline\n", encoding="utf-8")
        git(source, "add", "tracked")
        git(source, "commit", "-m", "baseline")
        assert_status(source, "clean-local", True)
        bundle = root / "config.bundle"
        git(source, "bundle", "create", str(bundle), "HEAD", "refs/heads/main")
        git(root, "clone", str(bundle), str(staging))
        assert_status(staging, "clean-tracking", True)
        lock = staging / "flake.lock"
        lock.write_text("{}\n", encoding="utf-8")
        assert_status(staging, "untracked", False)
        git(staging, "add", "flake.lock")
        assert_status(staging, "staged", False)
        git(staging, "commit", "-m", "lock inputs")
        assert_status(staging, "clean-ahead", True)
        (staging / "tracked").write_text("changed\n", encoding="utf-8")
        assert_status(staging, "modified", False)
        (staging / "tracked").write_text("baseline\n", encoding="utf-8")
        git(staging, "checkout", "--detach")
        assert_status(staging, "detached", False)
        git(staging, "checkout", "-b", "other")
        assert_status(staging, "wrong-branch", False)
