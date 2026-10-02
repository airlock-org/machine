"""Reconcile upstream Qubes appmenus for this GUI VM without starting guests.

The timer runs in the desktop user's systemd manager. Worker processes bound
qrexec timeouts and serialize with incoming SyncAppMenus RPCs. Qubes owns VM
membership, identities, policy and launching; its receiver owns sanitization
and icon decoding. This module only coordinates those existing mechanisms.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

LOG = logging.getLogger("airlock-appmenus")
VM_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")


def state_dir():
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "airlock-appmenus"
    )


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


@contextlib.contextmanager
def menu_lock(identity=None):
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = str(uuid.UUID(identity)) if identity else "inventory"
    with (directory / (name + ".lock")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def desktop_name(vm_uuid, appmenu_basename, *, disposable=False):
    # UUIDs survive VM rename, and keep favorites distinct even for identical
    # applications. Appmenu basenames have already passed Qubes sanitization.
    identity = str(uuid.UUID(vm_uuid))
    prefix = "org.qubes-os.dispvm" if disposable else "org.qubes-os.vm"
    return f"{prefix}.{identity}.{appmenu_basename}"


def changes(previous, current):
    """Only a complete successful Qubes inventory may withdraw cached menus."""
    removed = [
        item["name"] for identity, item in previous.items() if identity not in current
    ]
    renamed = [
        (previous[identity]["name"], item["name"])
        for identity, item in current.items()
        if identity in previous and previous[identity]["name"] != item["name"]
    ]
    return removed, renamed


class Backend:
    def __init__(self):
        import qubesadmin
        import qubesappmenus
        import qubesappmenus.receive

        self.app = qubesadmin.Qubes()
        self.receive = qubesappmenus.receive
        self.basedir = Path(qubesappmenus.basedir)

        class Menus(qubesappmenus.Appmenus):
            def desktop_name(self, vm, appmenu_basename, dispvm=False):
                return desktop_name(str(vm.uuid), appmenu_basename, disposable=dispvm)

        self.menus = Menus()

    def description(self, vm):
        if vm.name in ("dom0", self.app.local_name):
            return None
        if str(getattr(vm, "guivm", "")) != self.app.local_name:
            return None
        # Tags.__contains__ uses admin.vm.tag.Get, which the GUI policy denies.
        # Iteration uses the permitted read-only admin.vm.tag.List interface.
        if "airlock-retired" in set(vm.tags):
            return None
        if vm.features.get("internal", False):
            return None
        if vm.klass == "DispVM" and vm.auto_cleanup:
            return None
        return {
            "name": vm.name,
            "uuid": str(uuid.UUID(str(vm.uuid))),
            "running": vm.is_running(),
            "label": vm.label.color,
            "menu-items": vm.features.get("menu-items", None),
        }

    def inventory(self):
        # A denial/transport failure aborts the snapshot; it must not look like
        # a VM deletion. Remote Qubes API caches are refreshed on every cycle.
        return {
            item["uuid"]: item
            for vm in self.app.domains
            if (item := self.description(vm)) is not None
        }

    def cached_names(self):
        if not self.basedir.exists():
            return []
        return [
            path.name
            for path in self.basedir.iterdir()
            if path.is_dir() and not path.is_symlink() and VM_NAME.fullmatch(path.name)
        ]

    def remove(self, name):
        if not VM_NAME.fullmatch(name):
            raise ValueError("invalid cached VM name")
        self.menus.appmenus_remove(name)
        self.menus.appicons_remove(name)
        # Upstream leaves templates behind for later regeneration. Withdraw
        # those too when a VM is deleted, retired or moves to another GuiVM.
        import shutil

        if (self.basedir / name).exists():
            shutil.rmtree(self.basedir / name)

    def rename(self, old, new):
        source, destination = self.basedir / old, self.basedir / new
        if source.exists():
            if destination.exists():
                raise ValueError(f"catalog already exists for renamed VM {new}")
            source.rename(destination)

    def regenerate(self, name):
        vm = self.app.domains[name]
        if (self.basedir / name / "apps.templates").is_dir():
            self.menus.appmenus_update(vm)

    def sync(self, name, expected_uuid=None, *, receive_stdin=False, force=False):
        vm = self.app.domains[name]
        description = self.description(vm)
        if description is None or not description["running"]:
            raise ValueError("VM is not a running application VM for this GuiVM")
        if expected_uuid and description["uuid"] != expected_uuid:
            raise ValueError("VM identity changed during application refresh")

        class RunningVM:
            # Upstream GetAppmenus and image fetching default to autostart.
            # Explicitly disable it even if the guest stops during the scan.
            def __getattr__(self, key):
                return getattr(vm, key)

            def run_service(self, service, **kwargs):
                return vm.run_service(service, **{**kwargs, "autostart": False})

            def run_service_for_stdio(self, service, **kwargs):
                return vm.run_service_for_stdio(
                    service, **{**kwargs, "autostart": False}
                )

        running_vm = RunningVM()
        appmenus = self.receive.get_appmenus(None if receive_stdin else running_vm)
        # A second independent Qubes query prevents publishing after reassignment,
        # retirement or replacement while the guest was supplying its catalog.
        fresh_backend = Backend()
        fresh = fresh_backend.description(fresh_backend.app.domains[name])
        if fresh != description:
            raise ValueError("VM changed during application refresh")
        digest = hashlib.sha256(
            json.dumps([description, appmenus], sort_keys=True).encode()
        ).hexdigest()
        receipt = state_dir() / (description["uuid"] + ".json")
        previous = read_json(receipt, {})
        now = time.time()
        has_cache = (self.basedir / name / "apps.templates").is_dir()
        if (
            has_cache
            and not force
            and previous.get("digest") == digest
            and now - previous.get("updated", 0) < 300
        ):
            return
        # Unlike upstream's CLI, a successful empty catalog is an uninstall
        # snapshot. It must remove stale application entries too.
        self.receive.process_appmenus_templates(self.menus, running_vm, appmenus)
        self.menus.appmenus_update(vm)
        write_json(receipt, {"digest": digest, "updated": now})


def reconcile(backend, refresh):
    current = backend.inventory()
    directory = state_dir()
    with menu_lock():
        previous = read_json(directory / "inventory.json", {})
        # Metadata changes serialize with RPC publishers for the affected VMs.
        # Catalog retrieval itself uses independent per-VM locks, so a hung
        # guest cannot starve the other refresh workers.
        with contextlib.ExitStack() as locks:
            for identity in sorted(set(previous) | set(current)):
                locks.enter_context(menu_lock(identity))
            removed, renamed = changes(previous, current)
            # The first cycle also cleans upstream catalogs from before this service
            # was installed. A failed inventory never reaches this cleanup.
            wanted_names = {item["name"] for item in current.values()}
            rename_sources = {old for old, _new in renamed}
            removed += [
                name
                for name in backend.cached_names()
                if name not in wanted_names and name not in rename_sources
            ]
            for name in dict.fromkeys(removed):
                backend.remove(name)
            for old, new in renamed:
                backend.rename(old, new)
            for identity, item in current.items():
                if previous.get(identity) != item:
                    backend.regenerate(item["name"])
            write_json(directory / "inventory.json", current)
    # Child workers have their own Qubes clients. A stuck guest is bounded and
    # cannot prevent others being scanned; failures preserve the old catalog.
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                refresh, [item for item in current.values() if item["running"]]
            )
        )


def refresh_worker(item, *, timeout=90):
    command = [sys.executable, __file__, "--sync", item["name"], "--uuid", item["uuid"]]
    # Qrexec and icon decoding can have children; kill the whole worker group
    # on timeout so it cannot keep the menu lock and block subsequent cycles.
    with subprocess.Popen(command, start_new_session=True) as process:
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            LOG.warning("Application refresh for %s timed out", item["name"])
            return
        if status:
            LOG.warning("Application refresh for %s failed (%s)", item["name"], status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reconcile", action="store_true")
    mode.add_argument("--sync", metavar="VM")
    mode.add_argument("--receive", action="store_true")
    parser.add_argument("--uuid")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    backend = Backend()
    if args.reconcile:
        reconcile(backend, refresh_worker)
    else:
        name = os.environ.get("QREXEC_REMOTE_DOMAIN") if args.receive else args.sync
        if not name or not VM_NAME.fullmatch(name):
            parser.error("a valid originating VM name is required")
        identity = str(backend.app.domains[name].uuid)
        with menu_lock(identity):
            backend.sync(
                name,
                args.uuid or identity,
                receive_stdin=args.receive,
                force=args.force,
            )


if __name__ == "__main__":
    main()
