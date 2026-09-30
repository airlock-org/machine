"""Read bounded Qubes properties and XRes client provenance, without VM RPCs."""

import json
import os
from pathlib import Path
import re
import signal
import sys

GUID = "@guid@"
UNKNOWN = {"kind": "unknown", "name": "Unknown VM", "color": None}
ABSENT = {"kind": "absent", "name": "Unknown VM", "color": None}
LOCAL = {"kind": "local", "name": "sys-gui", "color": None}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,30}\Z")


def resolve(properties, peer_executable):
    """Only the installed GUI daemon may supply a forwarded window's identity."""
    if peer_executable != GUID:
        if properties or not peer_executable:
            return UNKNOWN.copy()
        return LOCAL.copy()
    if not properties:
        return ABSENT.copy()
    name = properties.get("_QUBES_VMNAME")
    label = properties.get("_QUBES_LABEL")
    color = properties.get("_QUBES_LABEL_COLOR")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return UNKNOWN.copy()
    if type(label) is not int or not 1 <= label <= 8:
        return UNKNOWN.copy()
    # A readable, verified name is still useful if its colour is malformed.
    color = f"#{color:06x}" if type(color) is int and 0 <= color <= 0xFFFFFF else None
    return {"kind": "vm", "name": name, "color": color}


def read_window(xid):
    from Xlib import X, Xatom, display
    from Xlib.ext import res

    connection = display.Display()
    try:
        window = connection.create_resource_object("window", xid)
        # Fail on a destroyed XID even when its former client's resource range lives.
        window.get_attributes()
        properties = {}
        for name, expected_type, expected_format in [
            ("_QUBES_VMNAME", Xatom.STRING, 8),
            ("_QUBES_LABEL", Xatom.CARDINAL, 8),
            ("_QUBES_LABEL_COLOR", Xatom.CARDINAL, 32),
        ]:
            atom = connection.intern_atom(name, only_if_exists=True)
            if not atom:
                continue
            prop = window.get_property(atom, X.AnyPropertyType, 0, 64)
            if prop is None:
                continue
            properties[name] = None
            if prop.property_type != expected_type or prop.format != expected_format or prop.bytes_after:
                continue
            if name == "_QUBES_VMNAME":
                properties[name] = bytes(prop.value).decode("ascii", errors="strict")
            elif len(prop.value) == 1:
                properties[name] = int(prop.value[0])
        peer = None
        ids = connection.res_query_client_ids([
            {"client": xid, "mask": res.LocalClientPIDMask},
        ]).ids
        for entry in ids:
            if entry.spec.mask & res.LocalClientPIDMask and len(entry.value) == 1:
                pid = int(entry.value[0])
                if pid > 0 and Path(f"/proc/{pid}").stat().st_uid == os.getuid():
                    peer = os.readlink(f"/proc/{pid}/exe")
        return resolve(properties, peer)
    finally:
        connection.close()


def main():
    # Bound connection/protocol stalls independently of Shell's cancellation.
    signal.alarm(2)
    try:
        xid = int(sys.argv[1], 16)
        if not 0 < xid <= 0xFFFFFFFF:
            raise ValueError("invalid XID")
        result = read_window(xid)
    except Exception:
        result = UNKNOWN.copy()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
