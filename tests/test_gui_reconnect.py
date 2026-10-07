"""GUI-only restart planning and bounded preservation checks."""

from copy import deepcopy
import importlib
import shutil
import subprocess
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle/runtime/tools"))
planning = importlib.import_module("airlock_machine_deploy.planning")
gui = importlib.import_module("airlock_machine_deploy.gui_reconnect")


def topology():
    def domain(guivm=None):
        return {
            "class": "StandaloneVM", "template": None, "netvm": None,
            "prefs": {"guivm": guivm, "autostart": True}, "devices": {},
            "features": {"airlock-gui-reconnect": "1"}, "services": {},
            "backend": {"type": "nix-closure", "state": {},
                        "store": {"mode": "shared-closure"}},
        }
    return {"domains": {"sys-gui": domain(), "sys-agent": domain("sys-gui"),
                        "usr-home": domain("sys-gui")}}


def changed_gui(old):
    new = deepcopy(old)
    new["domains"]["sys-gui"]["backend"]["revision"] = "new"
    return new


def test_gui_update_preserves_unchanged_capable_clients():
    old = topology()
    result = planning.plan(old, changed_gui(old), set(old["domains"]))
    assert result["stop"] == ["sys-gui"]
    assert result["gui_reconnect"] == {"sys-gui": ["sys-agent", "usr-home"]}
    assert result["start"][0] == "sys-gui"


@pytest.mark.parametrize("reason", ["unsupported", "changed", "network", "audio", "device"])
def test_other_dependencies_still_restart(reason):
    old = topology()
    client = old["domains"]["sys-agent"]
    if reason == "unsupported":
        client["features"]["airlock-gui-reconnect"] = "0"
    elif reason == "network":
        client["netvm"] = "sys-gui"
    elif reason == "audio":
        client["prefs"]["audiovm"] = "sys-gui"
    elif reason == "device":
        client["devices"] = {"pci": [{"backend": "sys-gui"}]}
    new = changed_gui(old)
    if reason == "changed":
        new["domains"]["sys-agent"]["prefs"]["vcpus"] = 4
    result = planning.plan(old, new, set(old["domains"]))
    assert "sys-agent" in result["stop"]
    assert result["gui_reconnect"] == {"sys-gui": ["usr-home"]}


def test_network_provider_update_still_stops_clients():
    old = topology()
    old["domains"]["usr-home"]["netvm"] = "sys-agent"
    new = deepcopy(old)
    new["domains"]["sys-agent"]["backend"]["revision"] = "new"
    result = planning.plan(old, new, set(old["domains"]))
    assert set(result["stop"]) == {"sys-agent", "usr-home"}
    assert "gui_reconnect" not in result


def test_changed_gui_assignment_requires_client_restart():
    old = topology()
    new = changed_gui(old)
    new["domains"]["usr-home"]["prefs"]["guivm"] = None
    result = planning.plan(old, new, set(old["domains"]))
    assert "usr-home" in result["stop"]
    assert result["gui_reconnect"] == {"sys-gui": ["sys-agent"]}


class Commands:
    def __init__(self):
        self.calls = []
        self.boot = "same-boot"
        self.status = "connected 99"
        self.clients = {"sys-agent": "sys-gui"}

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "qvm-prefs":
            return self.clients.get(args[1], "")
        if args[0] == "qvm-features":
            return "1"
        if args[0] == "qvm-ls":
            return "sys-gui|99"
        if args[0] == "qrexec-client":
            return self.boot if "boot_id" in args[-1] else self.status
        return ""


def checker(commands):
    return gui.GUIReconnect(SimpleNamespace(command=commands,
                            running=lambda: {"sys-gui", *commands.clients}))


def test_planned_gui_shutdown_preserves_clients_and_uses_graceful_force():
    commands = Commands()
    reconnect = checker(commands)
    before = reconnect.snapshot({"sys-gui": ["sys-agent"]})
    reconnect.shutdown("sys-gui", ["sys-agent"])
    after = reconnect.verify(before)
    assert after["sys-agent"]["boot_id"] == "same-boot"
    assert after["sys-agent"]["gui_xid"] == "99"
    assert commands.calls.count(("qvm-shutdown", "--force", "--wait", "--timeout",
                                 "120", "sys-gui")) == 1
    assert not any(call[0] == "qvm-kill" for call in commands.calls)


def test_unexpected_attached_vm_blocks_forced_shutdown():
    commands = Commands()
    commands.clients["other"] = "sys-gui"
    with pytest.raises(gui.DeploymentError, match="unexpected running GUI client other"):
        checker(commands).shutdown("sys-gui", ["sys-agent"])
    assert not any(call[0] == "qvm-shutdown" for call in commands.calls)


def test_restarted_guest_is_not_preservation_success():
    commands = Commands()
    reconnect = checker(commands)
    before = reconnect.snapshot({"sys-gui": ["sys-agent"]})
    commands.boot = "different-boot"
    with pytest.raises(gui.DeploymentError, match="guest restarted"):
        reconnect.verify(before)


def test_stale_display_connection_times_out_without_restarting_guest(monkeypatch):
    commands = Commands()
    reconnect = checker(commands)
    before = reconnect.snapshot({"sys-gui": ["sys-agent"]})
    commands.status = "connected 42"
    clock = [0.0]
    monkeypatch.setattr(gui.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gui.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    with pytest.raises(gui.DeploymentError, match="GUI reconnection timed out"):
        reconnect.verify(before, timeout=3)
    assert not any(call[0] in {"qvm-shutdown", "qvm-start"} for call in commands.calls)


def test_resume_cannot_hide_an_intervening_guest_restart():
    commands = Commands()
    reconnect = checker(commands)
    before = reconnect.snapshot({"sys-gui": ["sys-agent"]})
    commands.boot = "different-boot"
    with pytest.raises(gui.DeploymentError, match="interrupted GUI update"):
        reconnect.snapshot({"sys-gui": ["sys-agent"]}, before)


@pytest.mark.skipif(shutil.which("cc") is None, reason="C compiler unavailable")
def test_native_buffer_rebinding_preserves_addresses_and_content(tmp_path):
    patch = (Path(__file__).resolve().parents[1] / "lifecycle/misc/patches"
             / "qubes-gui-agent-linux/0003-preserve-xorg-across-guivm-restart.patch").read_text()
    added = "\n".join(line[1:] for line in patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    start = added.index("Bool xf86_qubes_reconnect(uint32_t domid, Bool connected) {")
    stop = added.index("\n}", start) + 2
    function = added[start:stop]
    source = tmp_path / "rebind.c"
    source.write_text(r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
typedef int Bool;
#define TRUE 1
#define FALSE 0
#define XC_PAGE_SIZE 4096
#define X_INFO 0
struct xf86_qubes_pixmap {
    struct xf86_qubes_pixmap *next;
    size_t pages;
    uint32_t *refs;
    uint8_t *data;
    Bool shared;
};
struct driver { void *xgs; uint32_t gui_domid; int scrnIndex; } driver;
typedef struct driver *DUMMYPtr;
#define DUMMYScrn (&driver)
#define DUMMYPTR(x) (x)
static struct xf86_qubes_pixmap *all_pixmaps;
static Bool gui_detached;
static Bool fail_allocation;
static uint32_t granted_to;
#define xf86DrvMsg(...) ((void)0)
static void xf86_qubes_pixmap_remove_list_all(void) {}
static void *xengntshr_share_pages(void *handle, uint32_t domid, size_t pages,
                                 uint32_t *refs, int writable) {
    (void)handle; (void)writable;
    if (fail_allocation) return NULL;
    granted_to = domid;
    for (size_t i = 0; i < pages; i++) refs[i] = domid + i;
    return mmap(NULL, pages * XC_PAGE_SIZE, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
}
''' + function + r'''
int main(void) {
    uint32_t refs[2] = {1, 2};
    struct xf86_qubes_pixmap priv = {.pages=2, .refs=refs, .shared=TRUE};
    priv.data = mmap(NULL, 8192, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(priv.data != MAP_FAILED);
    memset(priv.data, 42, 8192);
    uint8_t *original = priv.data;
    all_pixmaps = &priv;
    assert(xf86_qubes_reconnect(10, FALSE));
    assert(priv.data == original && gui_detached && !priv.shared);
    for (size_t i = 0; i < 8192; i++) assert(priv.data[i] == 42);
    memset(priv.data, 43, 8192); /* application keeps drawing while detached */
    fail_allocation = TRUE;
    assert(!xf86_qubes_reconnect(99, TRUE));
    assert(priv.data == original && priv.data[8191] == 43);
    fail_allocation = FALSE;
    assert(xf86_qubes_reconnect(99, TRUE));
    assert(!gui_detached && priv.shared && granted_to == 99);
    assert(priv.data == original && priv.refs[0] == 99 && priv.refs[1] == 100);
    for (size_t i = 0; i < 8192; i++) assert(priv.data[i] == 43);
    assert(!xf86_qubes_reconnect(65536, TRUE));
    munmap(priv.data, 8192);
    return 0;
}
''')
    executable = tmp_path / "rebind"
    result = subprocess.run(
        [shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
         str(source), "-o", str(executable)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    subprocess.run([str(executable)], check=True, timeout=10)
