"""Qubes GUI window-state forwarding is patched consistently on both sides."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH_ROOT = ROOT / "lifecycle/misc/patches"


def patch_text(package: str, name: str) -> str:
    return (PATCH_ROOT / package / name).read_text(encoding="utf-8")


def added_code(package, name):
    """Compile the implementation carried by the patch, not a Python model."""
    return "\n".join(
        line[1:]
        for line in patch_text(package, name).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def postimage_code(package, name):
    lines = []
    in_hunk = False
    for line in patch_text(package, name).splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith("diff --git"):
            in_hunk = False
        elif in_hunk and line.startswith((" ", "+")):
            lines.append(line[1:])
    return "\n".join(lines)


def function_block(code):
    depth = 0
    opened = False
    for index, char in enumerate(code):
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return code[: index + 1]
    raise ValueError("incomplete C implementation in protocol patch")


@pytest.fixture(scope="module")
def protocol_runner(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip(
            "C compiler is not installed; prepare it before running protocol tests"
        )
    maximize = added_code(
        "qubes-gui-daemon", "0001-gui-daemon-apply-maximize-window-flags.patch"
    )
    maximize = maximize[maximize.index("const uint32_t maximize_flags") :]
    move = added_code(
        "qubes-gui-daemon",
        "0002-gui-daemon-apply-interactive-window-move-requests.patch",
    )
    move = move[move.index("static void handle_window_moveresize") :]
    # The following patches add declarations/dispatch wiring after the function.
    move = function_block(move)
    branches = []
    for name in (
        "0001-gui-agent-forward-minimize-and-maximize-requests.patch",
        "0002-gui-agent-forward-interactive-window-move-requests.patch",
    ):
        code = postimage_code("qubes-gui-agent-linux", name)
        branches.append(
            function_block(code[code.index("} else if (ev->message_type") + 2 :])
        )
    agent = (
        "static void forward(Ghandles *g, XClientMessageEvent *ev) { if (0) {} "
        + " ".join(branches)
        + " }"
    )
    flags = "\n".join(
        line[1:]
        for line in patch_text(
            "qubes-gui-common", "0001-protocol-add-maximize-window-flags.patch"
        ).splitlines()
        if line.startswith((" #define WINDOW_FLAG_", "+#define WINDOW_FLAG_"))
    )
    wire = added_code(
        "qubes-gui-common", "0002-protocol-add-interactive-window-move-message.patch"
    )
    wire = wire[wire.index("struct msg_window_moveresize") :]
    directory = tmp_path_factory.mktemp("gui-protocol")
    source = directory / "protocol.c"
    source.write_text(
        PROTOCOL_STUBS
        + flags
        + "\n"
        + wire
        + "\n"
        + move
        + "\n"
        + "static void maximize(Ghandles *g, struct msg_window_flags msg) {\n"
        + "XClientMessageEvent ev = {0}; uint32_t flags_all = msg.flags_set | msg.flags_unset;\n"
        + maximize
        + "\n}\n"
        + agent
        + "\n"
        + PROTOCOL_MAIN
    )
    binary = directory / "protocol"
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Werror", str(source), "-o", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )

    def run(*args):
        result = subprocess.run(
            [str(binary), *map(str, args)], check=True, capture_output=True, text=True
        )
        return json.loads(result.stdout)

    return run


PROTOCOL_STUBS = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned long Window;
typedef struct { int type; void *display; Window window; unsigned long message_type;
    int format; union { long l[5]; } data; } XClientMessageEvent;
typedef XClientMessageEvent XEvent;
typedef struct { void *display, *vchan; Window root_win;
    unsigned long wm_moveresize, wm_state_maximized_vert, wm_state_maximized_horz, wm_change_state;
    uint32_t protocol_version; } Ghandles;
struct windowdata { int is_mapped, override_redirect; Window local_winid; };
struct msg_hdr { uint32_t window, type; };
struct msg_window_flags { uint32_t flags_set, flags_unset; };
#define MSG_WINDOW_FLAGS 1
#define MSG_WINDOW_MOVERESIZE 2
#define QUBES_GUID_MIN_WINDOW_MOVERESIZE 0x00010009
#define IconicState 3
#define False 0
#define ClientMessage 33
#define SubstructureNotifyMask 1
#define SubstructureRedirectMask 2
#define Button1Mask 256
static uint32_t direction, button;
static int pointer_ok = 1;
static unsigned int buttons;
static XClientMessageEvent events[8];
static int count, known_window = 1;
static void *windows_list;
static int lookup_window(Ghandles *g, void *list, Window window, const char *name) { return known_window; }
#define write_message(channel, hdr, msg) do { uint32_t values[2]; memcpy(values, &(msg), sizeof(values)); \
    events[count].data.l[0]=values[0]; events[count].data.l[1]=values[1]; \
    events[count].data.l[2]=(hdr).type; count++; } while (0)
#define read_struct(channel, msg) do { (msg).direction = direction; (msg).button = button; } while (0)
static int XQueryPointer(void *display, Window root, Window *rr, Window *cr,
    int *rx, int *ry, int *wx, int *wy, unsigned int *mask) {
    *rx = 120; *ry = 240; *mask = buttons; return pointer_ok;
}
static void XSendEvent(void *display, Window root, int propagate, long mask, XEvent *event) {
    events[count++] = *event;
}
"""
PROTOCOL_MAIN = r"""
int main(int argc, char **argv) {
    Ghandles g = {.root_win=1, .wm_moveresize=2, .wm_state_maximized_vert=3,
        .wm_state_maximized_horz=4};
    if (!strcmp(argv[1], "maximize")) {
        struct msg_window_flags msg = {.flags_set=strtoul(argv[2],0,0), .flags_unset=strtoul(argv[3],0,0)};
        maximize(&g, msg);
    } else if (!strcmp(argv[1], "agent")) {
        g.wm_change_state=5; g.protocol_version=strtoul(argv[2],0,0); known_window=atoi(argv[3]);
        XClientMessageEvent ev={.message_type=atoi(argv[4]), .format=atoi(argv[5]), .window=7};
        ev.data.l[0]=3; ev.data.l[2]=8; ev.data.l[3]=1; forward(&g, &ev);
    } else {
        direction=atoi(argv[2]); button=atoi(argv[3]); buttons=atoi(argv[4]);
        struct windowdata window = {.is_mapped=atoi(argv[5]), .override_redirect=atoi(argv[6]), .local_winid=7};
        pointer_ok=atoi(argv[7]); handle_window_moveresize(&g, &window);
    }
    printf("[");
    for (int i=0;i<count;i++) {
        if(i) printf(",");
        printf("[%ld,%ld,%ld,%ld]",events[i].data.l[0],events[i].data.l[1],events[i].data.l[2],events[i].data.l[3]);
    }
    printf("]\n"); return 0;
}
"""


@pytest.mark.parametrize(
    "setting,clearing,expected",
    [
        (24, 0, [[1, 3, 4, 0]]),
        (0, 24, [[0, 3, 4, 0]]),
        (8, 0, [[1, 3, 0, 0]]),
        (0, 16, [[0, 4, 0, 0]]),
        (0, 0, []),
    ],
)
def test_maximize_changes_both_axes_atomically(
    protocol_runner, setting, clearing, expected
):
    assert protocol_runner("maximize", setting, clearing) == expected


@pytest.mark.parametrize(
    "direction,button,buttons,mapped,override,pointer,expected",
    [
        (8, 1, 256, 1, 0, 1, [[120, 240, 8, 1]]),
        (8, 5, 4096, 1, 0, 1, [[120, 240, 8, 5]]),
        (7, 1, 256, 1, 0, 1, []),
        (8, 0, 256, 1, 0, 1, []),
        (8, 6, 256, 1, 0, 1, []),
        (8, 1, 0, 1, 0, 1, []),
        (8, 1, 256, 0, 0, 1, []),
        (8, 1, 256, 1, 1, 1, []),
        (8, 1, 256, 1, 0, 0, []),
    ],
)
def test_window_move_requires_a_trusted_pointer_and_mapped_window(
    protocol_runner, direction, button, buttons, mapped, override, pointer, expected
):
    assert (
        protocol_runner("move", direction, button, buttons, mapped, override, pointer)
        == expected
    )


@pytest.mark.parametrize(
    "version,known,message,format,expected",
    [
        (0x10009, 1, 2, 32, [[8, 1, 2, 0]]),
        (0x10008, 1, 2, 32, []),
        (0x10009, 0, 2, 32, []),
        (0x10009, 1, 2, 16, []),
        (0x10009, 1, 5, 32, [[4, 0, 1, 0]]),
        (0x10009, 0, 5, 32, []),
    ],
)
def test_agent_only_forwards_known_windows_and_supported_move_protocol(
    protocol_runner, version, known, message, format, expected
):
    assert protocol_runner("agent", version, known, message, format) == expected


@pytest.mark.nix
def test_window_state_overlay_patches_common_agent_and_daemon():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        pkgs = import f.inputs.nixpkgs {{
          system = "x86_64-linux";
          overlays = [
            f.inputs.nixos-qubes.overlays.default
            f.overlays.airlockQubesGuiWindowState
          ];
        }};
        patchNames = package: map builtins.baseNameOf package.patches;
      in {{
        common = patchNames pkgs.qubes-gui-common;
        agent = patchNames pkgs.qubes-gui-agent-linux;
        daemon = patchNames pkgs.qubes-gui-daemon;
        preparedGuestAgent = patchNames
          f.nixosConfigurations.airlock-domain-usr-home.config.nixpkgs.pkgs.qubes-gui-agent-linux;
        preparedGuiDaemon = patchNames
          f.nixosConfigurations.airlock-domain-sys-gui-usb-gui-lab.config.nixpkgs.pkgs.qubes-gui-daemon;
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)

    assert report == {
        "common": [
            "0001-protocol-add-maximize-window-flags.patch",
            "0002-protocol-add-interactive-window-move-message.patch",
        ],
        "agent": [
            "0001-gui-agent-forward-minimize-and-maximize-requests.patch",
            "0002-gui-agent-forward-interactive-window-move-requests.patch",
        ],
        "daemon": [
            "0001-gui-daemon-apply-maximize-window-flags.patch",
            "0002-gui-daemon-apply-interactive-window-move-requests.patch",
        ],
        "preparedGuestAgent": [
            "0001-gui-agent-forward-minimize-and-maximize-requests.patch",
            "0002-gui-agent-forward-interactive-window-move-requests.patch",
        ],
        "preparedGuiDaemon": [
            "0001-gui-daemon-apply-maximize-window-flags.patch",
            "0002-gui-daemon-apply-interactive-window-move-requests.patch",
            "0003-gui-daemon-leave-window-chrome-to-applications.patch",
        ],
    }
