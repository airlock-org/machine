from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
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


@pytest.mark.parametrize("label", [None, 0, 256, True, "6"])
def test_invalid_label_is_unknown(label):
    assert IDENTITY.resolve({**PROPERTIES, "_QUBES_LABEL": label}, IDENTITY.GUID) == IDENTITY.UNKNOWN


@pytest.mark.parametrize("label", [9, 255])
def test_custom_label_preserves_daemon_provenance(label):
    properties = {**PROPERTIES, "_QUBES_LABEL": label, "_QUBES_LABEL_COLOR": 0x6B7A3D}
    assert IDENTITY.resolve(properties, IDENTITY.GUID) == {
        "kind": "vm", "name": "usr-home", "color": "#6b7a3d",
    }
    assert IDENTITY.resolve(properties, "/nix/store/local/bin/xterm") == IDENTITY.UNKNOWN


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
assert.deepEqual(validateIdentity({{kind:'vm',name:'usr-home\\n',color:'#ffffff'}}), UNKNOWN);
assert.deepEqual(validateIdentity({{kind:'vm',name:['usr-home'],color:'#ffffff'}}), UNKNOWN);
assert.deepEqual(validateIdentity({{kind:'absent'}}), UNKNOWN);
assert.equal(validateIdentity({{kind:'vm',name:'usr-home',color:'red; width:10000px'}}).color, null);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_shell_update_cannot_publish_into_a_reenabled_instance(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime is not installed")
    state_module = tmp_path / "state.mjs"
    state_module.write_text((EXTENSION / "state.js").read_text())
    extension_module = tmp_path / "extension.mjs"
    source = re.sub(r"^import .*;\n", "", (EXTENSION / "extension.js").read_text(), flags=re.MULTILINE)
    extension_module.write_text(
        f"import {{FocusState, WindowIdentityCache, LOCAL, UNKNOWN}} from {json.dumps(state_module.as_uri())};\n"
        "const {Gio, Meta, Extension} = globalThis.shellMocks;\n" + source
    )
    script = f"""
import assert from 'node:assert/strict';
import {{FocusState, WindowIdentityCache}} from {json.dumps(state_module.as_uri())};
globalThis.shellMocks = {{
    Gio: {{Cancellable: class {{}}}},
    Meta: {{WindowType: {{DESKTOP: 1, DOCK: 2}}, WindowClientType: {{X11:0, WAYLAND:1}}}},
    Extension: class {{}},
}};
const {{default: Extension}} = await import({json.dumps(extension_module.as_uri())});
const extension = new Extension();
const renders = [];
const requests = [];
extension._state = new FocusState(value => renders.push(value));
extension._cache = new WindowIdentityCache();
extension._focused = {{get_window_type: () => 0, get_client_type: () => 0}};
extension._suppressed = () => false;
extension._cancelRead = () => {{}};
extension._resolve = () => new Promise(resolve => requests.push(resolve));
extension._update();
extension._state.disable();
extension._state = new FocusState(value => renders.push(value));
extension._update();
requests[1]({{kind:'vm',name:'usr-browser',color:'#cc0000'}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(renders.at(-1).name, 'usr-browser');
requests[0]({{kind:'vm',name:'usr-home',color:'#3465a4'}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(renders.at(-1).name, 'usr-browser');
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


def test_application_actors_and_settled_shell_focus(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime is not installed")
    state_module = tmp_path / "state.mjs"
    state_module.write_text((EXTENSION / "state.js").read_text())
    extension_module = tmp_path / "extension.mjs"
    source = re.sub(r"^import .*;\n", "", (EXTENSION / "extension.js").read_text(), flags=re.MULTILINE)
    extension_module.write_text(
        f"import {{FocusState, WindowIdentityCache, LOCAL, UNKNOWN}} from {json.dumps(state_module.as_uri())};\n"
        "const {Gio, GLib, Meta, Main, Extension} = globalThis.shellMocks;\n" + source
    )
    script = f"""
import assert from 'node:assert/strict';
import {{FocusState, WindowIdentityCache}} from {json.dumps(state_module.as_uri())};
let keyFocus = null;
const idle = [];
const removed = [];
const Main = {{overview: {{visible:false}}, sessionMode: {{isLocked:false,isGreeter:false}}, modalCount:0}};
globalThis.shellMocks = {{
    Gio: {{Cancellable: class {{}}}},
    GLib: {{PRIORITY_DEFAULT_IDLE:0, SOURCE_REMOVE:false,
        idle_add: (_priority, callback) => {{idle.push(callback); return idle.length;}},
        Source: {{remove: id => removed.push(id)}},
    }},
    Meta: {{WindowType: {{DESKTOP:1,DOCK:2}}, WindowClientType: {{X11:0, WAYLAND:1}}}},
    Main,
    Extension: class {{}},
}};
const calculator = {{get_window_type:()=>0, get_client_type:()=>0, get_transient_for:()=>null,
    connect:()=>1, disconnect:()=>{{}}}};
globalThis.global = {{stage:{{get_key_focus:()=>keyFocus}}, display:{{focus_window:calculator}},
    window_group:{{contains: actor=>actor.group==='window'}},
    top_window_group:{{contains: actor=>actor.group==='top-window'}},
}};
const {{default: Extension}} = await import({json.dumps(extension_module.as_uri())});
const extension = new Extension();
for (const actor of [null, {{group:'window'}}, {{group:'top-window'}}]) {{
    keyFocus=actor;
    assert.equal(extension._suppressed(), false);
}}
keyFocus={{group:'shell'}};
assert.equal(extension._suppressed(), true);
keyFocus=null;
Main.modalCount=1;
assert.equal(extension._suppressed(), true);
Main.modalCount=0;
Main.overview.visible=true;
assert.equal(extension._suppressed(), true);
Main.overview.visible=false;
Main.sessionMode.isLocked=true;
assert.equal(extension._suppressed(), true);
Main.sessionMode.isLocked=false;
const rendered=[];
extension._state=new FocusState(value=>rendered.push(value));
extension._signals=[];
extension._windowSignals=[];
extension._cache=new WindowIdentityCache();
extension._cancelRead=()=>{{}};
let reads=0;
extension._resolve=async()=>{{reads++; return {{kind:'vm',name:'usr-home',color:'#3465a4'}};}};
// Grab/key-focus notifications arrive while the menu close handler is running.
Main.modalCount=1;
keyFocus={{group:'shell'}};
extension._queueUpdate();
extension._queueUpdate();
assert.equal(idle.length,1);
assert.equal(rendered.at(-1),null);
assert.equal(reads,0);
// Complete the close handler before the idle callback; refresh the actual app.
Main.modalCount=0;
keyFocus={{group:'window'}};
idle[0]();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(reads,1);
assert.equal(rendered.at(-1).name,'usr-home');
// Disable removes an outstanding idle callback as well as pending reads/signals.
extension._indicator={{destroy:()=>{{}}}};
extension._queueUpdate();
extension.disable();
assert.deepEqual(removed,[2]);
assert.equal(rendered.at(-1),null);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cached_switches_retry_unknowns_and_window_lifetime(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime is not installed")
    state_module = tmp_path / "state.mjs"
    state_module.write_text((EXTENSION / "state.js").read_text())
    extension_module = tmp_path / "extension.mjs"
    source = re.sub(r"^import .*;\n", "", (EXTENSION / "extension.js").read_text(), flags=re.MULTILINE)
    extension_module.write_text(
        f"import {{FocusState, WindowIdentityCache, LOCAL, UNKNOWN}} from {json.dumps(state_module.as_uri())};\n"
        "const {Gio, Meta, Extension} = globalThis.shellMocks;\n" + source
    )
    script = f"""
import assert from 'node:assert/strict';
import {{FocusState, WindowIdentityCache, LOCAL, UNKNOWN}} from {json.dumps(state_module.as_uri())};
globalThis.shellMocks = {{
    Gio: {{Cancellable: class {{
        cancelled = false;
        cancel() {{this.cancelled = true;}}
        is_cancelled() {{return this.cancelled;}}
    }}}},
    Meta: {{WindowType: {{DESKTOP:1,DOCK:2}}, WindowClientType: {{X11:0,WAYLAND:1}}}},
    Extension: class {{}},
}};
const {{default: Extension}} = await import({json.dumps(extension_module.as_uri())});
const extension = new Extension();
const rendered = [];
const reads = [];
let suppressed = false;
function window(xid, clientType = 0) {{
    const signals = new Map();
    let id = 0;
    return {{
        parent: null,
        get_client_type: () => clientType,
        get_window_type: () => 0,
        get_description: () => `${{xid}} untrusted title`,
        get_transient_for() {{return this.parent;}},
        connect(signal, callback) {{signals.set(++id, callback); return id;}},
        disconnect(id) {{signals.delete(id);}},
        close() {{for (const callback of [...signals.values()]) callback();}},
        signalCount: () => signals.size,
    }};
}}
const home = {{kind:'vm',name:'usr-home',color:'#3465a4'}};
const browser = {{kind:'vm',name:'usr-browser',color:'#f57900'}};
extension._cache = new WindowIdentityCache();
extension._render = value => rendered.push(value);
extension._state = new FocusState(value => rendered.push(value));
extension._suppressed = () => suppressed;
extension._read = (xid, cancellable) => new Promise(resolve => reads.push({{xid,cancellable,resolve}}));
const settle = () => new Promise(resolve => setImmediate(resolve));
function focus(window) {{extension._focused = window; extension._update();}}
const files = window('0x123');
const firefox = window('0x456');
focus(files);
assert.deepEqual(rendered.at(-1), UNKNOWN);
reads.at(-1).resolve(home);
await settle();
focus(firefox);
assert.deepEqual(rendered.at(-1), UNKNOWN); // Never use the previous window's cache.
reads.at(-1).resolve(browser);
await settle();
const warmStart = rendered.length;
for (let i = 0; i < 100; i++) {{focus(files); focus(firefox);}}
assert.equal(reads.length, 2); // 200 repeat switches spawn no reader.
assert.ok(rendered.slice(warmStart).every(value => value.kind === 'vm'));
assert.deepEqual(rendered.slice(warmStart, warmStart + 2), [home, browser]);
suppressed = true;
focus(firefox);
assert.equal(rendered.at(-1), null);
suppressed = false;
focus(firefox);
assert.deepEqual(rendered.at(-1), browser);
assert.equal(reads.length, 2);
focus(null);
assert.equal(rendered.at(-1), null);
const localStart = rendered.length;
focus(window('unused', 1));
assert.deepEqual(rendered.slice(localStart), [LOCAL]);
assert.equal(reads.length, 2);
// Closure invalidates an unfocused entry; reusing its XID does not reuse identity.
files.close();
assert.equal(files.signalCount(), 0);
const replacement = window('0x123');
focus(replacement);
assert.deepEqual(rendered.at(-1), UNKNOWN);
assert.equal(reads.length, 3);
reads.at(-1).resolve(UNKNOWN);
await settle();
focus(replacement);
assert.equal(reads.length, 4); // Unknown is retried, never cached.
reads.at(-1).resolve(home);
await settle();
// Changing a transient's parent must resolve the current parent, not a cached one.
const dialog = window('0x789');
dialog.parent = replacement;
focus(dialog);
reads.at(-1).resolve({{kind:'absent'}});
await settle();
assert.deepEqual(rendered.at(-1), home);
dialog.parent = firefox;
focus(dialog);
reads.at(-1).resolve({{kind:'absent'}});
await settle();
assert.deepEqual(rendered.at(-1), browser);
// Rapid focus changes cancel cold reads; late results cannot populate that cache.
const racing = window('0xabc');
focus(racing);
const cancelled = reads.at(-1);
focus(firefox);
cancelled.resolve(home);
await settle();
assert.deepEqual(rendered.at(-1), browser);
assert.equal(extension._cache.get(racing), null);
// A window destroyed during its read cannot be resurrected by the completion.
focus(racing);
const closing = reads.at(-1);
racing.close();
closing.resolve(home);
await settle();
assert.equal(extension._cache.get(racing), null);
// Disable/re-enable cannot leak a late result into either old or new caches.
const pending = window('0xdef');
focus(pending);
const late = reads.at(-1);
const oldCache = extension._cache;
oldCache.clear();
extension._state.disable();
extension._cache = new WindowIdentityCache();
extension._render = value => rendered.push(value);
extension._state = new FocusState(value => rendered.push(value));
focus(firefox);
assert.deepEqual(rendered.at(-1), UNKNOWN);
late.resolve(home);
await settle();
assert.equal(oldCache.get(pending), null);
assert.equal(extension._cache.get(pending), null);
assert.deepEqual(rendered.at(-1), UNKNOWN);
assert.equal(pending.signalCount(), 0);
assert.equal(replacement.signalCount(), 0);
reads.at(-1).resolve(browser);
await settle();
assert.deepEqual(rendered.at(-1), browser);
extension._cache.clear();
assert.equal(firefox.signalCount(), 0);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
