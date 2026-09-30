from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "lifecycle/misc/modules/sys-gui/desktop/gnome/focused-vm-extension"
spec = importlib.util.spec_from_file_location("focused_vm_identity", EXTENSION / "identity.py")
IDENTITY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IDENTITY)
PROPERTIES = {"_QUBES_VMNAME": "usr-home", "_QUBES_LABEL": 6, "_QUBES_LABEL_COLOR": 0x3465A4}


@pytest.mark.parametrize("name", ["usr-home", "usr-browser", "a", "a" * 31])
def test_daemon_identity(name):
    result = IDENTITY.resolve({**PROPERTIES, "_QUBES_VMNAME": name}, IDENTITY.GUID)
    assert result == {"kind": "vm", "name": name, "color": "#3465a4"}


@pytest.mark.parametrize("name", [None, "", "1vm", "a" * 32, "<b>vm</b>", "vm\n", "vm\x00", "a b"])
def test_invalid_names_are_unknown(name):
    assert IDENTITY.resolve({**PROPERTIES, "_QUBES_VMNAME": name}, IDENTITY.GUID) == IDENTITY.UNKNOWN


@pytest.mark.parametrize("label", [None, 0, 9, True, "6"])
def test_invalid_label_is_unknown(label):
    assert IDENTITY.resolve({**PROPERTIES, "_QUBES_LABEL": label}, IDENTITY.GUID) == IDENTITY.UNKNOWN


@pytest.mark.parametrize("color", [None, -1, 0x1000000, True, "red; width:10000px"])
def test_bad_colour_does_not_hide_verified_name(color):
    assert IDENTITY.resolve({**PROPERTIES, "_QUBES_LABEL_COLOR": color}, IDENTITY.GUID) == {
        "kind": "vm", "name": "usr-home", "color": None,
    }


def test_absence_and_local_provenance():
    assert IDENTITY.resolve({}, IDENTITY.GUID) == IDENTITY.ABSENT
    assert IDENTITY.resolve({}, None) == IDENTITY.UNKNOWN
    assert IDENTITY.resolve({}, "/nix/store/local/bin/xterm") == IDENTITY.LOCAL
    assert IDENTITY.resolve(PROPERTIES, "/nix/store/local/bin/xterm") == IDENTITY.UNKNOWN
    assert IDENTITY.resolve(PROPERTIES, None) == IDENTITY.UNKNOWN
    assert IDENTITY.resolve({"_QUBES_VMNAME": None}, IDENTITY.GUID) == IDENTITY.UNKNOWN


@pytest.mark.parametrize("fault", [None, "destroyed", "wrong-type", "wrong-format", "oversized", "bad-ascii", "missing-peer", "missing-properties"])
def test_bounded_x11_reads_and_failures(monkeypatch, fault):
    atoms = {name: index for index, name in enumerate(PROPERTIES, 1)}
    props = {
        1: SimpleNamespace(property_type=31, format=8, bytes_after=0, value=b"usr-home"),
        2: SimpleNamespace(property_type=6, format=8, bytes_after=0, value=b"\x06"),
        3: SimpleNamespace(property_type=6, format=32, bytes_after=0, value=[0x3465A4]),
    }
    if fault == "wrong-type":
        props[1].property_type = 6
    if fault == "wrong-format":
        props[2].format = 32
    if fault == "oversized":
        props[1].bytes_after = 1
    if fault == "bad-ascii":
        props[1].value = b"\xff"
    if fault == "missing-properties":
        props = {}
    closed = []

    def attributes():
        if fault == "destroyed":
            raise RuntimeError("BadWindow")

    def property_read(atom, kind, offset, length):
        assert (kind, offset, length) == (0, 0, 64)
        return props.get(atom)

    window = SimpleNamespace(get_attributes=attributes, get_property=property_read)
    connection = SimpleNamespace(
        create_resource_object=lambda kind, xid: window,
        intern_atom=lambda name, only_if_exists: atoms[name],
        res_query_client_ids=lambda specs: SimpleNamespace(ids=[] if fault == "missing-peer" else [
            SimpleNamespace(spec=SimpleNamespace(mask=2), value=[123]),
        ]),
        close=lambda: closed.append(True),
    )
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(
        X=SimpleNamespace(AnyPropertyType=0),
        Xatom=SimpleNamespace(STRING=31, CARDINAL=6),
        display=SimpleNamespace(Display=lambda: connection),
    ))
    monkeypatch.setitem(sys.modules, "Xlib.ext", SimpleNamespace(res=SimpleNamespace(LocalClientPIDMask=2)))
    monkeypatch.setattr(IDENTITY.Path, "stat", lambda path: SimpleNamespace(st_uid=IDENTITY.os.getuid()))
    monkeypatch.setattr(IDENTITY.os, "readlink", lambda path: IDENTITY.GUID)
    if fault in {"destroyed", "bad-ascii"}:
        with pytest.raises((RuntimeError, UnicodeDecodeError)):
            IDENTITY.read_window(0x123)
    else:
        result = IDENTITY.read_window(0x123)
        assert result["kind"] == ("vm" if fault is None else "absent" if fault == "missing-properties" else "unknown")
    assert closed == [True]


def test_focus_changes_suppression_and_late_results(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime is not installed; quick tests never provision tools")
    # Use an mjs copy so this also works with Node versions before module detection.
    module = tmp_path / "state.mjs"
    module.write_text((EXTENSION / "state.js").read_text())
    script = f"""
import assert from 'node:assert/strict';
import {{FocusState, LOCAL, UNKNOWN, validateIdentity}} from {json.dumps(module.as_uri())};
const rendered = [];
const state = new FocusState(value => rendered.push(value));
const first = state.begin('files', false);
state.complete(first, {{kind:'vm', name:'usr-home', color:'#3465a4'}});
assert.equal(rendered.at(-1).name, 'usr-home');
const second = state.begin('browser', false);
assert.deepEqual(rendered.at(-1), UNKNOWN);
state.complete(first, {{kind:'vm', name:'usr-home', color:'#3465a4'}});
assert.deepEqual(rendered.at(-1), UNKNOWN);
state.complete(second, {{kind:'vm', name:'usr-browser', color:'#cc0000'}});
assert.equal(rendered.at(-1).name, 'usr-browser');
const menu = state.begin('browser', true);
assert.equal(rendered.at(-1), null);
state.complete(second, LOCAL);
assert.equal(rendered.at(-1), null);
const empty = state.begin(null, false);
assert.equal(rendered.at(-1), null);
state.complete(menu, LOCAL);
assert.equal(rendered.at(-1), null);
const local = state.begin('terminal', false);
state.complete(local, LOCAL);
assert.deepEqual(rendered.at(-1), LOCAL);
state.disable();
state.complete(local, UNKNOWN);
state.complete(empty, LOCAL);
assert.equal(rendered.at(-1), null);
assert.deepEqual(validateIdentity({{kind:'vm',name:'<b>bad</b>',color:'#ffffff'}}), UNKNOWN);
assert.deepEqual(validateIdentity({{kind:'absent'}}), UNKNOWN);
assert.equal(validateIdentity({{kind:'vm',name:'usr-home',color:'red; width:10000px'}}).color, null);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_declarative_installation_and_enablement():
    nix = (EXTENSION.parent / "default.nix").read_text()
    assert '"airlock-focused-vm@airlock.local"' in nix
    assert "focusedVMExtension\n" in nix
    assert "cp extension.js state.js metadata.json stylesheet.css" in nix
    assert "ps.python-xlib" in nix
    assert '${pkgs.qubes-gui-daemon}/bin/qubes-guid' in nix
    assert (EXTENSION / "metadata.json").read_text().count('"50"') == 1
