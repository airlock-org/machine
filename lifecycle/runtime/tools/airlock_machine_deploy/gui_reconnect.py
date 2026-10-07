"""Preserve capable guests across a planned restart of their assigned GuiVM."""

import subprocess
import time

from .common import DeploymentError


class GUIReconnect:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.command = coordinator.command

    def guest_read(self, name, path, timeout=10):
        return self.command("qrexec-client", "-d", name, "user:cat " + path,
                            timeout=timeout).strip()

    def snapshot(self, groups, previous=None):
        snapshots = {}
        for provider, clients in groups.items():
            if provider != "sys-gui":
                raise DeploymentError("GUI preservation only supports sys-gui")
            for name in clients:
                if self.command("qvm-prefs", name, "guivm") != provider:
                    raise DeploymentError(f"{name}: assigned GuiVM differs from plan")
                if self.command("qvm-features", name, "airlock-gui-reconnect") != "1":
                    raise DeploymentError(f"{name}: GUI reconnection is not installed")
                boot = self.guest_read(name, "/proc/sys/kernel/random/boot_id")
                if not boot:
                    raise DeploymentError(f"{name}: missing boot identity")
                if previous and name in previous and boot != previous[name]["boot_id"]:
                    raise DeploymentError(f"{name}: guest restarted during interrupted GUI update")
                snapshots[name] = {"provider": provider, "boot_id": boot}
        return snapshots

    def shutdown(self, provider, clients):
        # --force bypasses Qubes' GUI dependency guard, not ACPI shutdown. Only
        # this planned restart can use it, after checking every live attachment.
        for name in self.coordinator.running():
            if name == provider:
                continue
            if (self.command("qvm-prefs", name, "guivm") == provider
                    and name not in clients):
                raise DeploymentError(f"{provider}: unexpected running GUI client {name}")
        self.command("qvm-shutdown", "--force", "--wait", "--timeout", "120",
                     provider, timeout=150)

    def verify(self, snapshots, timeout=180):
        observed = {}
        for name, snapshot in snapshots.items():
            provider = snapshot["provider"]
            row = self.command("qvm-ls", "--raw-data", "--fields", "NAME,XID",
                               provider).split("|")
            if len(row) != 2 or row[0] != provider or not row[1].isdecimal():
                raise DeploymentError(f"{provider}: cannot determine running Xen identity")
            xid = row[1]
            deadline = time.monotonic() + timeout
            last_error = "display not connected"
            while time.monotonic() < deadline:
                try:
                    remaining = max(0.1, min(10, deadline - time.monotonic()))
                    boot = self.guest_read(name, "/proc/sys/kernel/random/boot_id", remaining)
                    if boot != snapshot["boot_id"]:
                        raise DeploymentError(f"{name}: guest restarted during GUI update")
                    status = self.guest_read(name, "/run/qubes/gui-reconnect-status", remaining)
                    if status == "connected " + xid:
                        observed[name] = {**snapshot, "gui_xid": xid, "status": status}
                        break
                    last_error = status
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    last_error = str(exc)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            else:
                raise DeploymentError(f"{name}: GUI reconnection timed out: {last_error}")
        return observed
