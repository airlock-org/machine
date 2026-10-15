from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from .storage_engine import _make_storage_closure, _make_storage_object, _run_storage, _storage_env
from .support import _run, _write_executable


def _write_fake_qubes_package(fake_root: Path) -> None:
    qubes_dir = fake_root / "qubes"
    qubes_dir.mkdir(parents=True)
    (qubes_dir / "exc.py").write_text(
        "class StoragePoolException(Exception):\n    pass\n", encoding="utf-8"
    )
    (qubes_dir / "storage.py").write_text(
        textwrap.dedent("""
            class Pool:
                driver = "fake"

                def __init__(self, *, name, revisions_to_keep=1, ephemeral_volatile=False):
                    self.name = name
                    self.revisions_to_keep = revisions_to_keep
                    self.ephemeral_volatile = ephemeral_volatile

                @property
                def config(self):
                    return {
                        "name": self.name,
                        "driver": self.driver,
                        "revisions_to_keep": self.revisions_to_keep,
                        "ephemeral_volatile": self.ephemeral_volatile,
                    }

                def init_volume(self, vm, volume_config):
                    volume_config["pool"] = self
                    volume = Volume(**volume_config)
                    vm.volumes[volume.name] = volume
                    return volume


            class BasicPool(Pool):
                driver = "file"


            class Volume:
                devtype = "disk"
                domain = None
                path = None

                def __init__(
                    self,
                    name,
                    pool,
                    vid=None,
                    *,
                    revisions_to_keep=0,
                    rw=False,
                    save_on_stop=False,
                    size=0,
                    snap_on_start=False,
                    source=None,
                    **kwargs,
                ):
                    self.name = name
                    self.pool = pool
                    self.vid = vid or name
                    self.revisions_to_keep = revisions_to_keep
                    self.rw = rw
                    self.save_on_stop = save_on_stop
                    self._size = int(size)
                    self.snap_on_start = snap_on_start
                    self.source = source
                    for key, value in kwargs.items():
                        setattr(self, key, value)

                @property
                def size(self):
                    return self._size

                @property
                def config(self):
                    payload = {
                        "name": self.name,
                        "pool": self.pool.name,
                        "vid": self.vid,
                        "size": self.size,
                        "rw": self.rw,
                        "snap_on_start": self.snap_on_start,
                        "save_on_stop": self.save_on_stop,
                        "source": self.source,
                        "revisions_to_keep": self.revisions_to_keep,
                    }
                    for key in (
                        "domain_name",
                        "generation",
                        "closure_hash",
                        "view_kind",
                        "view_path",
                        "manifest_path",
                        "blob_map_path",
                        "blob_logical_size",
                    ):
                        if hasattr(self, key):
                            payload[key] = getattr(self, key)
                    return payload

                async def create(self):
                    return self


            class Storage:
                def __init__(self, vm):
                    self.vm = vm

                def init_volume(self, name, volume_config):
                    pool = self.vm.app.get_pool(volume_config["pool"])
                    volume = pool.init_volume(self.vm, volume_config.copy())
                    self.vm.volumes[name] = volume
                    return volume
            """).lstrip(),
        encoding="utf-8",
    )
    (qubes_dir / "__init__.py").write_text(
        textwrap.dedent("""
            import json
            import os

            from . import storage


            class VM:
                klass = "StandaloneVM"

                def __init__(self, app, name, data=None):
                    self.app = app
                    self.name = name
                    self.features = (data or {}).get("features", {})
                    self.volume_config = {}
                    self.volumes = {}
                    self.storage = storage.Storage(self)
                    if data is None:
                        root_pool = app.get_pool("default")
                        root = storage.Volume(
                            name="root",
                            pool=root_pool,
                            vid="root",
                            size=1024,
                            rw=True,
                            snap_on_start=False,
                            save_on_stop=True,
                            source=None,
                            revisions_to_keep=1,
                        )
                        self.volumes["root"] = root
                        self.volume_config["root"] = root.config
                    else:
                        self.volume_config = data.get("volume_config", {})
                        for name, config in self.volume_config.items():
                            self.storage.init_volume(name, config.copy())

                def is_running(self):
                    return os.environ.get("AIRLOCK_FAKE_QUBES_RUNNING", "0") in (
                        "1",
                        "true",
                        "yes",
                    )


            class Qubes:
                def __init__(self):
                    self.pools = {"default": storage.BasicPool(name="default")}
                    self.domains = {}
                    state = os.environ["AIRLOCK_FAKE_QUBES_STATE_JSON"]
                    domain = os.environ["AIRLOCK_FAKE_QUBES_DOMAIN"]
                    if os.path.exists(state):
                        with open(state, "r", encoding="utf-8") as handle:
                            data = json.load(handle)
                        for name, config in data.get("pools", {}).items():
                            if name == "default":
                                continue
                            if config.get("driver") == "airlock-closure":
                                from airlock_qubes_storage.qubes import ClosurePool

                                self.pools[name] = ClosurePool(
                                    name=name,
                                    root_dir=config["root_dir"],
                                    revisions_to_keep=config.get("revisions_to_keep", 0),
                                    ephemeral_volatile=config.get(
                                        "ephemeral_volatile", False
                                    ),
                                )
                        self.domains = {
                            domain: VM(self, domain, data.get("domains", {}).get(domain, {}))
                        }
                    else:
                        self.domains = {domain: VM(self, domain)}

                def get_pool(self, name):
                    return self.pools[name]

                async def add_pool(self, name, **kwargs):
                    if kwargs.get("driver") != "airlock-closure":
                        raise RuntimeError(f"unsupported pool driver {kwargs.get('driver')}")
                    from airlock_qubes_storage.qubes import ClosurePool

                    pool = ClosurePool(
                        name=name,
                        root_dir=kwargs["root_dir"],
                        revisions_to_keep=kwargs.get("revisions_to_keep", 0),
                        ephemeral_volatile=kwargs.get("ephemeral_volatile", False),
                    )
                    await pool.setup()
                    self.pools[name] = pool
                    return pool

                def save(self, lock=True):
                    payload = {
                        "pools": {
                            name: pool.config for name, pool in sorted(self.pools.items())
                        },
                        "domains": {},
                    }
                    for name, vm in sorted(self.domains.items()):
                        payload["domains"][name] = {
                            "features": vm.features,
                            "volume_config": {
                                volume_name: volume.config
                                for volume_name, volume in sorted(vm.volumes.items())
                            },
                        }
                    for target in (
                        os.environ["AIRLOCK_FAKE_QUBES_SAVE_JSON"],
                        os.environ["AIRLOCK_FAKE_QUBES_STATE_JSON"],
                    ):
                        with open(target, "w", encoding="utf-8") as handle:
                            json.dump(payload, handle, sort_keys=True)

                def close(self):
                    for vm in self.domains.values():
                        del vm.app
                    return None
            """).lstrip(),
        encoding="utf-8",
    )


def check_nix_domain_storage_helper_shared(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    work_dir = log_dir / "work"
    nix_root = work_dir / "nix-root"
    physical_store = nix_root / "nix/store"
    pool = work_dir / "pool"
    fake_root = work_dir / "fake"
    fake_bin = work_dir / "bin"
    state_json = work_dir / "fake-qubes-state.json"
    save_json = result_dir / "fake-qubes-save.json"
    systemctl_log = result_dir / "systemctl.log"

    if nix_root.exists():
        _run(["chmod", "-R", "u+w", str(nix_root)], check=False)
    shutil.rmtree(result_dir, ignore_errors=True)
    for path in (physical_store, work_dir / "closures", fake_bin):
        path.mkdir(parents=True)
    root_image = physical_store / "example-root" / "root.img"
    root_image.parent.mkdir()
    with root_image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(1080)
        stream.write(b"\x53\xef")
    _write_fake_qubes_package(fake_root)
    _write_executable(
        fake_bin / "systemctl",
        """
        #!/bin/sh
        printf 'systemctl %s\\n' "$*" >> "$AIRLOCK_FAKE_SYSTEMCTL_LOG"
        case "$1" in
          is-active|stop|start)
            exit 0
            ;;
        esac
        exit 1
        """,
    )

    entries = {
        "common": "11111111111111111111111111111111-common",
        "system": "22222222222222222222222222222222-system",
        "unique": "33333333333333333333333333333333-unique",
        "system2": "44444444444444444444444444444444-system-2",
        "unique2": "55555555555555555555555555555555-unique-2",
    }
    for entry in entries.values():
        _make_storage_object(physical_store, entry, entry, size=8192)
    closure = _make_storage_closure(
        work_dir,
        "helper-shared",
        [entries["common"], entries["system"], entries["unique"]],
    )
    closure_gen2 = _make_storage_closure(
        work_dir,
        "helper-shared-gen2",
        [entries["common"], entries["system2"], entries["unique2"]],
    )

    env = _storage_env(repo_root, str(fake_bin))
    env.update(
        {
            "PYTHONPATH": f"{fake_root}:{repo_root / 'lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src'}",
            "AIRLOCK_NIX_ROOT": str(nix_root),
            "AIRLOCK_CLOSURE_STORE_VERIFY": "tree",
            "AIRLOCK_FAKE_QUBES_DOMAIN": "nix-native-work",
            "AIRLOCK_FAKE_QUBES_SAVE_JSON": str(save_json),
            "AIRLOCK_FAKE_QUBES_STATE_JSON": str(state_json),
            "AIRLOCK_FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        }
    )

    def run_helper(
        name: str,
        args: list[str],
        *,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
        command_env = env.copy()
        if extra_env:
            command_env.update(extra_env)
        result = _run(
            [
                "python3",
                str(
                    repo_root
                    / "lifecycle/runtime/tools/scripts/nix-domain-storage.py"
                ),
                *args,
            ],
            cwd=repo_root,
            env=command_env,
            stdout=result_dir / f"{name}.json",
            stderr=result_dir / f"{name}.err",
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"helper command failed: {' '.join(args)}\n"
                f"{(result_dir / f'{name}.err').read_text(encoding='utf-8')}"
            )
        payload = None
        if (result_dir / f"{name}.json").stat().st_size:
            payload = json.loads((result_dir / f"{name}.json").read_text())
        return result, payload

    ensure_args = [
        "ensure-store",
        "nix-native-work",
        str(closure),
        "/nix/store/example-source",
        "--mode",
        "shared-closure",
        "--system",
        f"/nix/store/{entries['system']}",
        "--root",
        "/nix/store/example-root",
        "--kernel",
        "kernel-a",
        "--pool-root",
        str(pool),
    ]
    _result, first = run_helper("helper-output", ensure_args)
    assert first is not None
    assert first["schema"] == "airlock.nix-domain-storage.v1"
    assert first["domain"] == "nix-native-work"
    assert first["mode"] == "shared-closure"
    assert first["pool"] == "airlock-closure"
    assert first["pool_created"] is True
    assert first["volume_created"] is True
    assert first["view_created"] is True
    assert first["metadata_changed"] is True
    assert first["qubesd_restart_required"] is False
    assert first["qubesd_restarted"] is True
    assert first["generation"] == 1
    assert first["filesystem"] == "erofs"
    assert first["view_backend"] == "erofs-objectmap"
    assert first["objects_total"] == 3
    assert first["objects_admitted"] == 3
    assert first["objects_reused"] == 0

    view_image = Path(str(first["metadata_image"]))
    blob_map = Path(str(first["blob_map"]))
    assert view_image.is_file()
    assert blob_map.is_file()
    assert view_image.stat().st_mode & 0o222 == 0

    saved = json.loads(save_json.read_text(encoding="utf-8"))
    volumes = saved["domains"]["nix-native-work"]["volume_config"]
    assert saved["pools"]["airlock-closure"]["driver"] == "airlock-closure"
    assert volumes["store"]["pool"] == "airlock-closure"
    assert volumes["store"]["rw"] is False
    assert volumes["store"]["snap_on_start"] is False
    assert volumes["store"]["save_on_stop"] is True
    assert volumes["store"]["source"] is None
    assert volumes["store"]["generation"] == "1"
    assert volumes["store"]["view_kind"] == "metadata-image"
    assert volumes["store"]["view_path"]
    assert volumes["store"]["manifest_path"]
    assert volumes["store-data"]["pool"] == "airlock-closure"
    assert volumes["store-data"]["rw"] is False
    assert volumes["store-data"]["snap_on_start"] is False
    assert volumes["store-data"]["save_on_stop"] is True
    assert volumes["store-data"]["source"] is None
    assert volumes["store-data"]["generation"] == "1"
    assert volumes["store-data"]["view_kind"] == "erofs-blob-map"
    assert volumes["store-data"]["blob_map_path"]
    assert int(volumes["store-data"]["blob_logical_size"]) > 0

    state = json.loads(state_json.read_text(encoding="utf-8"))
    state["domains"]["nix-native-work"]["features"] = {
        "airlock-store-source": "/nix/store/example-source",
        "airlock-store-mode": "shared-closure",
        "airlock-store-view-backend": str(first["view_backend"]),
        "airlock-store-generation": str(first["generation"]),
        "airlock-store-closure-hash": str(first["closure_hash"]),
        "airlock-store-view-image": str(first["view_image"]),
    }
    state_json.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    _result, second = run_helper("helper-output-2", ensure_args)
    assert second is not None
    assert second["mode"] == "shared-closure"
    assert second["volume_created"] is False
    assert second["view_created"] is False
    assert second["metadata_changed"] is False
    assert second["qubesd_restarted"] is False
    assert second["view_backend"] == "erofs-objectmap"
    assert str(second["generation"]) == "1"

    ensure_gen2_args = [
        "ensure-store",
        "nix-native-work",
        str(closure_gen2),
        "/nix/store/example-source-2",
        "--mode",
        "shared-closure",
        "--system",
        f"/nix/store/{entries['system2']}",
        "--root",
        "/nix/store/example-root",
        "--kernel",
        "kernel-b",
        "--pool-root",
        str(pool),
    ]
    _result, gen2 = run_helper("helper-output-gen2", ensure_gen2_args)
    assert gen2 is not None
    assert gen2["mode"] == "shared-closure"
    assert gen2["generation"] == 2
    assert gen2["objects_total"] == 3
    assert gen2["objects_admitted"] == 2
    assert gen2["objects_reused"] == 1
    assert gen2["view_backend"] == "erofs-objectmap"

    running, _payload = run_helper(
        "helper-switch-running",
        ["switch-store", "nix-native-work", "1", "--pool-root", str(pool)],
        extra_env={"AIRLOCK_FAKE_QUBES_RUNNING": "1"},
        check=False,
    )
    assert running.returncode != 0
    assert "shared store generation switch requested but domain is running" in (
        result_dir / "helper-switch-running.err"
    ).read_text(encoding="utf-8")

    _result, switched = run_helper(
        "helper-switch-back",
        ["switch-store", "nix-native-work", "1", "--pool-root", str(pool)],
    )
    assert switched is not None
    assert switched["mode"] == "shared-closure"
    assert switched["generation"] == 1
    assert switched["engine_current_generation"] == 1
    assert switched["volume_created"] is False
    assert switched["metadata_changed"] is True
    assert switched["qubesd_restarted"] is True
    assert switched["system"] == f"/nix/store/{entries['system']}"
    assert switched["kernel_name"] == "kernel-a"

    state = json.loads(state_json.read_text(encoding="utf-8"))
    volumes = state["domains"]["nix-native-work"]["volume_config"]
    assert volumes["store"]["generation"] == "1"
    assert volumes["store"]["view_path"] == str(view_image)
    assert volumes["store-data"]["generation"] == "1"
    assert volumes["store-data"]["blob_map_path"] == str(blob_map)

    state["domains"]["nix-native-work"]["features"] = {
        "airlock-store-source": "/nix/store/example-source-2",
        "airlock-store-mode": "shared-closure",
        "airlock-store-view-backend": "erofs-objectmap",
        "airlock-store-generation": str(gen2["generation"]),
        "airlock-store-closure-hash": str(gen2["closure_hash"]),
        "airlock-store-view-image": str(gen2["view_image"]),
        "airlock-store-blob-map": str(gen2["blob_map"]),
    }
    state_json.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    _result, repaired = run_helper("helper-stale-repair", ensure_gen2_args)
    assert repaired is not None
    assert repaired["mode"] == "shared-closure"
    assert repaired["generation"] == 2
    assert repaired["engine_current_generation"] == 2
    assert repaired["volume_created"] is False
    assert repaired["view_created"] is False
    assert repaired["metadata_changed"] is True
    assert repaired["qubesd_restarted"] is True
    assert repaired["objects_admitted"] == 0
    assert repaired["objects_reused"] == 3

    state = json.loads(state_json.read_text(encoding="utf-8"))
    volumes = state["domains"]["nix-native-work"]["volume_config"]
    assert volumes["store"]["generation"] == "2"
    assert volumes["store"]["view_path"] == str(gen2["view_image"])
    assert volumes["store-data"]["generation"] == "2"
    assert volumes["store-data"]["blob_map_path"] == str(gen2["blob_map"])

    _result, status = _run_storage(
        repo_root,
        result_dir,
        "engine-status-after-switch",
        ["status", "--pool-root", str(pool)],
    )
    assert status["domains"]["nix-native-work"]["current"] == 2
    assert len(status["domains"]["nix-native-work"]["generations"]) == 2
    assert systemctl_log.read_text(encoding="utf-8") == (
        "systemctl is-active --quiet qubesd.service\n"
        "systemctl stop qubesd.service\n"
        "systemctl start qubesd.service\n"
        "systemctl is-active --quiet qubesd.service\n"
        "systemctl stop qubesd.service\n"
        "systemctl start qubesd.service\n"
        "systemctl is-active --quiet qubesd.service\n"
        "systemctl stop qubesd.service\n"
        "systemctl start qubesd.service\n"
        "systemctl is-active --quiet qubesd.service\n"
        "systemctl stop qubesd.service\n"
        "systemctl start qubesd.service\n"
    )


def check_closure_store_linux_storage(repo_root: Path, log_dir: Path) -> None:
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    result_path = log_dir / "result.json"
    if os.geteuid() != 0:
        result_path.write_text(
            json.dumps(
                {
                    "schema": "airlock.closure-store-linux-storage.v1",
                    "status": "skipped",
                    "reason": "root or passwordless sudo is required for loop, device-mapper, and mount namespace coverage",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        import pytest

        pytest.skip("root is required for real loop/device-mapper storage coverage")

    for command in ("dmsetup", "losetup", "mount", "umount", "unshare", "blockdev", "mkfs.erofs", "mkfs.ext4"):
        if shutil.which(command) is None:
            raise AssertionError(f"missing required command: {command}")
    result = _run(
        ["unshare", "--mount", "--propagation", "private", sys.executable,
         str(repo_root / "tests/closure_store_linux_probe.py"),
         str(repo_root / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src")],
        cwd=repo_root,
    )
    report = json.loads(result.stdout)
    assert report["status"] == "passed" and report["devices_cleaned_up"]
    result_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")


def check_closure_volume_filtered_device(repo_root: Path, log_dir: Path) -> None:
    work_dir = log_dir / "work"
    result_path = log_dir / "result.json"
    shutil.rmtree(log_dir, ignore_errors=True)
    (work_dir / "pool/objects/segments").mkdir(parents=True)
    storage_src = (
        repo_root
        / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"
    )
    sys.path.insert(0, str(storage_src))
    original_modules = {
        name: sys.modules.get(name) for name in ("qubes", "qubes.exc", "qubes.storage")
    }

    class StoragePoolException(Exception):
        pass

    class FakePoolBase:
        def __init__(
            self,
            *,
            name: str,
            revisions_to_keep: int = 0,
            ephemeral_volatile: bool = False,
            **_kwargs: object,
        ) -> None:
            self.name = name
            self.revisions_to_keep = revisions_to_keep
            self.ephemeral_volatile = ephemeral_volatile

    class FakeBlockDevice:
        def __init__(
            self,
            path: str,
            name: str,
            script: str | None = None,
            rw: bool = True,
            domain: str | None = None,
            devtype: str = "disk",
        ) -> None:
            self.path = path
            self.name = name
            self.script = script
            self.rw = rw
            self.domain = domain
            self.devtype = devtype

    class FakeVolumeBase:
        def __init__(self, **kwargs: object) -> None:
            self.name = kwargs.get("name", "")
            self.pool = kwargs.get("pool")
            self.vid = kwargs.get("vid", "")
            self.rw = bool(kwargs.get("rw", True))
            self.snap_on_start = bool(kwargs.get("snap_on_start", False))
            self.save_on_stop = bool(kwargs.get("save_on_stop", False))
            self.source = kwargs.get("source")
            self.revisions_to_keep = kwargs.get("revisions_to_keep", 0)
            self._size = int(kwargs.get("size", 0))
            self.path = kwargs.get("path")

        @property
        def config(self) -> dict[str, object]:
            pool = self.pool.name if hasattr(self.pool, "name") else self.pool
            return {
                "name": self.name,
                "pool": pool,
                "vid": self.vid,
                "rw": self.rw,
                "snap_on_start": self.snap_on_start,
                "save_on_stop": self.save_on_stop,
                "source": self.source,
                "revisions_to_keep": self.revisions_to_keep,
                "size": self._size,
            }

        def block_device(self) -> FakeBlockDevice:
            return FakeBlockDevice(self.path, self.name, rw=self.rw)

    qubes_module = types.ModuleType("qubes")
    exc_module = types.ModuleType("qubes.exc")
    storage_module = types.ModuleType("qubes.storage")
    exc_module.StoragePoolException = StoragePoolException
    storage_module.Pool = FakePoolBase
    storage_module.Volume = FakeVolumeBase
    storage_module.BlockDevice = FakeBlockDevice
    qubes_module.exc = exc_module
    qubes_module.storage = storage_module
    sys.modules["qubes"] = qubes_module
    sys.modules["qubes.exc"] = exc_module
    sys.modules["qubes.storage"] = storage_module

    try:
        from airlock_qubes_storage._engine import views
        from airlock_qubes_storage.qubes import ClosurePool

        pool_root = work_dir / "pool"
        segment_blob = pool_root / "objects/segments/000001.blob"
        segment_blob.write_bytes(b"segment")
        blob_map = (
            pool_root / "domains/sys-config-vault/generations/1/store.blob-map.json"
        )
        blob_map.parent.mkdir(parents=True, exist_ok=True)
        blob_map.write_text(
            json.dumps(
                {
                    "schema": "airlock.closure-store.blob-map.v1",
                    "segments": [
                        {
                            "id": "000001",
                            "blob": str(segment_blob),
                            "size": segment_blob.stat().st_size,
                        }
                    ],
                    "logical_size": 512,
                    "extents": [],
                }
            ),
            encoding="utf-8",
        )
        manifest = blob_map.parent / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")

        pool = ClosurePool(name="airlock-closure", root_dir=str(pool_root))
        vm = types.SimpleNamespace(name="sys-config-vault")
        volume = pool.init_volume(
            vm,
            {
                "name": "store-data",
                "pool": "airlock-closure",
                "vid": "domains/sys-config-vault/generations/1/store-data",
                "size": 512,
                "rw": False,
                "snap_on_start": False,
                "save_on_stop": True,
                "source": None,
                "revisions_to_keep": 0,
                "domain_name": "sys-config-vault",
                "generation": "1",
                "closure_hash": "abc",
                "view_kind": "erofs-blob-map",
                "view_path": "/dev/mapper/stale-airlock-config-vault-1-abc",
                "blob_map_path": str(blob_map),
                "blob_logical_size": 512,
                "manifest_path": str(manifest),
            },
        )
        original_attach = views.attach_blob_view
        calls: list[dict[str, str]] = []

        def filtered_attach(
            root_dir: str, domain: str, generation: str
        ) -> dict[str, object]:
            calls.append(
                {"root_dir": root_dir, "domain": domain, "generation": generation}
            )
            return {
                "device": "/dev/mapper/airlock-config-vault-1-abc",
                "backing_paths": [str(segment_blob)],
            }

        views.attach_blob_view = filtered_attach
        device = volume.block_device()
        assert device.path == "/dev/mapper/airlock-config-vault-1-abc"
        assert os.path.realpath(device.path) != os.path.realpath(segment_blob)
        assert calls == [
            {
                "root_dir": str(pool_root),
                "domain": "sys-config-vault",
                "generation": "1",
            }
        ]
        assert "view_path" not in volume.config

        volume.path = "/dev/mapper/stale-airlock-config-vault-1-abc"
        device = volume.block_device()
        assert device.path == "/dev/mapper/airlock-config-vault-1-abc"
        assert calls == [
            {
                "root_dir": str(pool_root),
                "domain": "sys-config-vault",
                "generation": "1",
            },
            {
                "root_dir": str(pool_root),
                "domain": "sys-config-vault",
                "generation": "1",
            },
        ]

        def unsafe_attach(
            _root_dir: str, _domain: str, _generation: str
        ) -> dict[str, object]:
            return {
                "device": str(segment_blob),
                "backing_paths": [str(segment_blob)],
            }

        views.attach_blob_view = unsafe_attach
        volume.path = None
        try:
            volume.block_device()
        except StoragePoolException as exc:
            assert "global Airlock backing storage" in str(exc)
        else:
            raise AssertionError("global backing storage was exposed to the guest")
        finally:
            views.attach_blob_view = original_attach

        result_path.write_text(
            json.dumps(
                {
                    "schema": "airlock.closure-volume-filtered-device.v1",
                    "status": "passed",
                    "guest_device": device.path,
                    "backing_paths": [str(segment_blob)],
                    "backing_storage_exposed": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        result = json.loads(result_path.read_text())
        assert result["schema"] == "airlock.closure-volume-filtered-device.v1"
        assert result["status"] == "passed"
        assert result["guest_device"] == "/dev/mapper/airlock-config-vault-1-abc"
        assert result["backing_storage_exposed"] is False
    finally:
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
