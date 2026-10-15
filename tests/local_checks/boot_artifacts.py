from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from .support import _physical_store_path, _run


def check_nix_closure_domain_volume_list(repo_root: Path, log_dir: Path) -> None:
    script = repo_root / "lifecycle/misc/tools/scripts/nix-closure-domain-smoke.sh"
    work_dir = log_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    volume_list = work_dir / "qvm-volume-list.txt"
    volume_list.write_text(
        "POOL:VOLUME                                       VMNAME                "
        "VOLUME_NAME  REVERT_POSSIBLE\n"
        "linux-kernel:6.12.64-1.fc41                       airlock-closure-smoke  "
        "kernel       No\n"
        "varlibqubes:appvms/airlock-closure-smoke/private   "
        "airlock-closure-smoke  private      No\n"
        "varlibqubes:appvms/airlock-closure-smoke/root      "
        "airlock-closure-smoke  root         No\n"
        "varlibqubes:appvms/airlock-closure-smoke/store     "
        "airlock-closure-smoke  store        No\n"
        "varlibqubes:appvms/airlock-closure-smoke/store-data "
        "airlock-closure-smoke store-data   No\n"
        "varlibqubes:appvms/airlock-closure-smoke/volatile  "
        "airlock-closure-smoke  volatile     No\n",
        encoding="utf-8",
    )

    rows = volume_list.read_text(encoding="utf-8").splitlines()[1:]
    assert any(
        len(parts := row.split()) >= 3
        and (parts[2] == "store" or parts[0].endswith("/store"))
        for row in rows
    )

    script_text = script.read_text(encoding="utf-8")
    assert 'grep -F "$DOMAIN:store"' not in script_text
    assert 'qvm_volume_list_has_volume "$DOMAIN" store' in script_text
    assert 'qvm_volume_list_has_volume "$DOMAIN" store-data' in script_text
    assert 'touch "$home_dir/airlock-closure-smoke-home"' in script_text
    assert "touch /rw/airlock-closure-smoke-persistent" not in script_text

    assert re.search(
        r"^store_options=([^,\n]+,)*ro(,|$)",
        "store_options=ro,nodev,relatime\n",
        re.MULTILINE,
    )
    assert not re.search(
        r"^store_options=([^,\n]+,)*ro(,|$)",
        "store_options=nodev,rw,relatime\n",
        re.MULTILINE,
    )


def check_nix_closure_root_image(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    shutil.rmtree(result_dir, ignore_errors=True)
    result_dir.mkdir(parents=True)

    _run(
        [
            "nix",
            "build",
            "--out-link",
            str(result_dir / "root-image"),
            f"{repo_root}#airlock-nix-closure-root-image-test",
        ],
        cwd=repo_root,
        stdout=result_dir / "nix-build.stdout",
        stderr=result_dir / "nix-build.stderr",
    )
    root_image_out = Path(
        _physical_store_path(repo_root, os.readlink(result_dir / "root-image"))
    )
    manifest = json.loads(
        (root_image_out / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "airlock.nix-closure-root-image.v1"
    assert manifest["contains_store"] is False
    assert manifest["store_mountpoint"] == "/nix/store"
    assert manifest["store_device_default"] == "/dev/xvde"

    root_files = (
        (root_image_out / "root-files.txt").read_text(encoding="utf-8").splitlines()
    )
    assert "./sbin/init" in root_files
    assert "./nix/store" in root_files
    assert not any(re.search(r"^\./nix/store/.+", item) for item in root_files)

    _run(
        ["debugfs", "-R", "ls -p /nix/store", str(root_image_out / "root.img")],
        stdout=result_dir / "debugfs-nix-store.txt",
        stderr=result_dir / "debugfs-nix-store.stderr",
    )
    debugfs_output = (result_dir / "debugfs-nix-store.txt").read_text(encoding="utf-8")
    assert not re.search(r"/[A-Za-z0-9]{32}-", debugfs_output)

    # Inspect actual image metadata, not the staging tree: unprivileged builds
    # must not leak their UID/GID into system directories, files, or symlinks.
    image_paths = ["/", "/lost+found"] + [item.removeprefix(".") for item in root_files]
    assert "/var" in image_paths
    for index, image_path in enumerate(image_paths):
        stat_log = result_dir / f"debugfs-inode-{index}.txt"
        _run(
            ["debugfs", "-R", f"stat {image_path}", str(root_image_out / "root.img")],
            stdout=stat_log,
            stderr=result_dir / f"debugfs-inode-{index}.stderr",
        )
        inode = stat_log.read_text(encoding="utf-8")
        ownership = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", inode)
        assert (
            ownership is not None
        ), f"Missing inode metadata for {image_path}: {inode}"
        assert ownership.groups() == (
            "0",
            "0",
        ), f"{image_path} must be owned by root:root in the guest image: {inode}"


def check_qubes_vm_kernel_stubdomain_boot(repo_root: Path, log_dir: Path) -> None:
    del log_dir
    repack = repo_root / "vendor/nixos-qubes/nix/repackRpmKernel.nix"
    install = (
        repo_root
        / "vendor/nixos-qubes/pkgs/by-name/ni/nixos-qubes-tools/src/nixos-qubes-install-kernel-rpm"
    )

    repack_text = repack.read_text(encoding="utf-8")
    install_text = install.read_text(encoding="utf-8")

    def assert_before_mkfs(text: str, copy_tokens: tuple[str, str]) -> None:
        mkfs_pos = text.index("mkfs.ext3")
        for token in copy_tokens:
            pos = text.index(token)
            assert pos < mkfs_pos

    assert "expects direct-boot artifacts at its root" in repack_text
    assert "srcs = [" in repack_text
    assert "kernelSrc" in repack_text
    assert "modulesSrc" in repack_text
    assert_before_mkfs(
        repack_text,
        (
            'cp "var/lib/qubes/vm-kernels/${version}/vmlinuz" imgroot/vmlinuz',
            'cp "var/lib/qubes/vm-kernels/${version}/initramfs" imgroot/initramfs',
        ),
    )

    assert "expects direct-boot artifacts at its root" in install_text
    assert_before_mkfs(
        install_text,
        (
            'cp "$kerneldir/vmlinuz" "$imgroot/vmlinuz"',
            'cp "$kerneldir/initramfs" "$imgroot/initramfs"',
        ),
    )


def check_qubes_libvirt_hvm_pirq_patch(repo_root: Path, log_dir: Path) -> None:
    del log_dir
    package = (
        repo_root
        / "vendor/nixos-qubes/pkgs/by-name/qu/qubes-core-libvirt/package.nix"
    )
    patch = package.with_name("0019-libxl-enable-hvm-pirq-for-pci-passthrough.patch")

    package_text = package.read_text(encoding="utf-8")
    patch_text = patch.read_text(encoding="utf-8")

    assert (
        "patch -p1 --input ${./0019-libxl-enable-hvm-pirq-for-pci-passthrough.patch}"
        in package_text
    )
    assert "#ifdef LIBXL_HAVE_HVM_PIRQ" in patch_text
    assert "def->os.type == VIR_DOMAIN_OSTYPE_HVM" in patch_text
    assert "d_config->num_pcidevs > 0" in patch_text
    assert "libxl_defbool_set(&d_config->b_info.u.hvm.pirq, true);" in patch_text
