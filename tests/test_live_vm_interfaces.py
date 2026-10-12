"""Installed interfaces needed before a new VM's host generation boots."""

import ast
import filecmp
import io
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.nix


def test_icon_rpc_selects_largest_suitable_packaged_bitmap():
    expression = f'''
      let
        f = builtins.getFlake "{ROOT}";
        cfg = f.nixosConfigurations.airlock-domain-usr-home.config;
        pkgs = cfg.nixpkgs.pkgs;
        handler = cfg.environment.etc."qubes-rpc/qubes.GetImageRGBA".source;
      in pkgs.runCommand "airlock-icon-size-selection" {{
        XDG_DATA_DIRS = "${{pkgs.hicolor-icon-theme}}/share:${{pkgs.firefox}}/share";
      }} ''
        printf 'xdgicon:firefox\\n' | ${{handler}} > "$out"
      ''
    '''
    result = subprocess.run(
        [str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
         "build", "--impure", "--no-link", "--print-out-paths", "--expr", expression],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    output = Path(os.environ["AIRLOCK_NIX_ROOT"]) / result.stdout.strip().lstrip("/")
    dimensions, pixels = output.read_bytes().split(b"\n", 1)
    assert tuple(map(int, dimensions.split())) == (128, 128)
    assert len(pixels) == 128 * 128 * 4


def test_icon_rpc_renders_complex_application_svgs():
    expression = f'''
      let
        f = builtins.getFlake "{ROOT}";
        cfg = f.nixosConfigurations.airlock-domain-usr-home.config;
        pkgs = cfg.nixpkgs.pkgs;
        handler = cfg.environment.etc."qubes-rpc/qubes.GetImageRGBA".source;
      in pkgs.runCommand "airlock-complex-svg-icons" {{}} ''
        mkdir -p "$out"
        printf '%s\\n' "${{pkgs.gnome-contacts}}/share/icons/hicolor/scalable/apps/org.gnome.Contacts.svg" | ${{handler}} > "$out/contacts.rgba"
        printf '%s\\n' "${{pkgs.gnome-logs}}/share/icons/hicolor/scalable/apps/org.gnome.Logs.svg" | ${{handler}} > "$out/logs.rgba"
        printf '%s\\n' "${{pkgs.gnome-font-viewer}}/share/icons/hicolor/scalable/apps/org.gnome.font-viewer.svg" | ${{handler}} > "$out/fonts.rgba"
      ''
    '''
    result = subprocess.run(
        [str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
         "build", "--impure", "--no-link", "--print-out-paths", "--expr", expression],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    output = Path(os.environ["AIRLOCK_NIX_ROOT"]) / result.stdout.strip().lstrip("/")
    for name in ("contacts", "logs", "fonts"):
        dimensions, pixels = (output / f"{name}.rgba").read_bytes().split(b"\n", 1)
        width, height = map(int, dimensions.split())
        assert 0 < width <= 512 and 0 < height <= 512
        assert len(pixels) == width * height * 4


@pytest.fixture(scope="module")
def interfaces():
    expression = f'''
      let
        f = builtins.getFlake "{ROOT}";
        gui = f.nixosConfigurations.airlock-domain-sys-gui.config;
        host = f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration;
        find = name: packages: builtins.head (builtins.filter
          (p: (p.pname or p.name) == name) packages);
        desktop = find "qubes-desktop-linux-common" gui.environment.systemPackages;
        policy = find "airlock-sys-gui-policy" host.virtualisation.qubes.packages;
        controlPolicy = find "43-airlock-ctrl-guest.policy" host.virtualisation.qubes.packages;
      in {{
        sync = builtins.readFile "${{find "airlock-sync-qube-appmenus" gui.environment.systemPackages}}/bin/airlock-sync-qube-appmenus";
        receiver = builtins.readFile gui.environment.etc."qubes-rpc/qubes.SyncAppMenus".source;
        receiverUser = gui.environment.etc."qubes/rpc-config/qubes.SyncAppMenus".text;
        desktop = builtins.readFile "${{desktop}}/${{gui.nixpkgs.pkgs.python3.sitePackages}}/qubesappmenus/receive.py";
        menus = builtins.readFile "${{desktop}}/${{gui.nixpkgs.pkgs.python3.sitePackages}}/qubesappmenus/__init__.py";
        policy = builtins.readFile "${{policy}}/etc/qubes/policy.d/50-airlock-sys-gui.policy";
        qrexecPythonPath = "${{gui.nixpkgs.pkgs.python3.pkgs.qubes-core-qrexec}}/${{gui.nixpkgs.pkgs.python3.sitePackages}}";
        controlPolicy = builtins.readFile "${{controlPolicy}}/etc/qubes/policy.d/43-airlock-ctrl-guest.policy";
        prepare = builtins.readFile host.environment.etc."qubes-rpc/airlock.Ctrl.MachinePrepare".source;
        candidate = builtins.readFile host.environment.etc."qubes-rpc/airlock.Pool.AdmitCandidate".source;
      }}
    '''
    result = subprocess.run([str(ROOT / "lifecycle/misc/tools/scripts/airlock-nix.sh"),
                             "eval", "--impure", "--json", "--expr", expression],
                            cwd=ROOT, check=True, text=True, capture_output=True)
    report = json.loads(result.stdout)
    directory = Path(os.environ["AIRLOCK_LOG_DIR"]) / "live-vm-interfaces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "interfaces.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def test_new_vm_menu_sync_starts_guest_then_uses_upstream_tools(interfaces, tmp_path):
    trace = tmp_path / "trace.jsonl"
    helper = tmp_path / "helper"
    helper.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
with Path(os.environ['TRACE']).open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'remote': os.environ.get('QREXEC_REMOTE_DOMAIN')}) + '\\n')
sys.exit(1 if os.environ.get('FAIL_READY') and 'Applications' in sys.argv else 0)
''')
    helper.chmod(0o755)
    source = re.sub(r"/nix/store/[^/]+/bin/(airlock-launch-qube-app|airlock-application-menus)",
                    shlex.quote(str(helper)), interfaces["sync"])
    source = re.sub(r"/nix/store/[^/]+/bin/timeout", "timeout", source)
    for failure in (False, True):
        trace.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-c", source, "sync", "usr-new"],
            env={**os.environ, "TRACE": str(trace), "QREXEC_REMOTE_DOMAIN": "dom0",
                 "FAIL_READY": "1" if failure else ""}, capture_output=True, text=True)
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        assert calls[0]["args"] == ["Applications", "usr-new"]
        if failure:
            assert result.returncode != 0
            assert len(calls) == 1
        else:
            assert result.returncode == 0, result.stderr
            assert calls[1] == {"args": ["--sync", "usr-new", "--force"], "remote": None}
    trace.unlink()
    result = subprocess.run(["bash", "-c", source, "sync"],
                            env={**os.environ, "TRACE": str(trace)}, capture_output=True)
    assert result.returncode != 0
    assert not trace.exists()


def test_generated_menu_entries_use_session_barrier_and_fixed_rpc(interfaces):
    assert "Exec=airlock-launch-qube-app Application %VMNAME% " in interfaces["desktop"]
    assert "qubes.StartApp+{}" in interfaces["desktop"]
    assert "airlock.AppSessionReady + sys-gui @tag:guivm-sys-gui allow" in interfaces["policy"]
    assert "airlock.AppSessionReady * @anyvm @anyvm deny" in interfaces["policy"]
    assert "force-user='user'" in interfaces["receiverUser"]
    result = subprocess.run(["bash", "-c", interfaces["receiver"]],
                            env={**os.environ, "QREXEC_SERVICE_ARGUMENT": "unexpected"},
                            capture_output=True)
    assert result.returncode == 2


def test_capture_policy_precedes_gui_default_deny_and_is_narrow(interfaces, monkeypatch):
    package_path = Path(os.environ["AIRLOCK_NIX_ROOT"]) / interfaces["qrexecPythonPath"].lstrip("/")
    monkeypatch.syspath_prepend(str(package_path))
    from qrexec.exc import AccessDenied
    from qrexec.policy.parser import AllowResolution, Request, StringPolicy

    policy = StringPolicy(policy=interfaces["policy"])
    system_info = {"domains": {name: {"name": name, "type": "StandaloneVM",
        "tags": [], "uuid": str(index), "power_state": "Running"}
        for index, name in enumerate(("dom0", "sys-gui", "usr-home", "usr-other"))}}
    for service in ("airlock.CaptureImport", "airlock.CaptureOpen"):
        for argument in ("+image", "+video"):
            request = Request(service, argument, "sys-gui", "usr-home", system_info=system_info)
            assert isinstance(policy.evaluate(request), AllowResolution)
        for argument, source, target in (("+other", "sys-gui", "usr-home"),
                ("+image", "sys-gui", "usr-other"), ("+image", "usr-other", "usr-home"),
                ("+image", "sys-gui", "dom0")):
            with pytest.raises(AccessDenied):
                policy.evaluate(Request(service, argument, source, target, system_info=system_info))


def test_preparation_and_admission_are_controller_only(interfaces):
    for service in ("airlock.Ctrl.MachinePrepare", "airlock.Pool.AdmitCandidate"):
        assert f"{service} * @anyvm @anyvm deny" in interfaces["controlPolicy"]
    assert "airlock.Ctrl.MachinePrepare + sys-ctrl dom0 allow" in interfaces["controlPolicy"]
    assert "airlock.Pool.AdmitCandidate * sys-ctrl dom0 allow" in interfaces["controlPolicy"]
    for handler in (interfaces["prepare"], interfaces["candidate"]):
        for caller in ("usr-new", "sys-gui", "usr-assistant", ""):
            result = subprocess.run(["bash", "-c", handler],
                                    env={**os.environ, "QREXEC_REMOTE_DOMAIN": caller},
                                    capture_output=True)
            assert result.returncode == 1


def receiver_namespace(source):
    # Exercise functions from the actual patched package without connecting
    # its Qubes parser or importing the host's qrexec/image dependencies.
    names = {"required_fields", "required_fields_legacy", "appmenus_line_size",
             "appmenus_line_count", "std_re", "fields_regexp", "CATEGORIES_WHITELIST"}
    functions = {"sanitise_categories", "get_appmenus", "create_template"}
    body = [node for node in ast.parse(source).body
            if (isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets))
            or (isinstance(node, ast.FunctionDef) and node.name in functions)]
    namespace = {"re": re, "os": os, "sys": sys, "shlex": shlex,
                 "qubesappmenus": SimpleNamespace(AppmenusSubdirs=SimpleNamespace(icons_subdir="apps.icons"))}
    exec(compile(ast.Module(body=body, type_ignores=[]), "packaged-receiver", "exec"), namespace)
    return namespace


def test_packaged_receiver_preserves_search_metadata_and_uses_fixed_launch_rpc(interfaces, tmp_path, monkeypatch):
    receiver = receiver_namespace(interfaces["desktop"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "example.desktop:Name=Example\n"
        "example.desktop:Name[fr]=Éditeur\n"
        "example.desktop:Keywords=graphics;painting;\n"
        "example.desktop:OnlyShowIn=GNOME;\n"
        "example.desktop:Icon=example\n"
        "example.desktop:Exec=touch /tmp/must-not-run\n"
        "example.desktop:X-Airlock-Source=/nix/store/package/share/applications/example.desktop\n"
    ))
    entries = receiver["get_appmenus"](None)
    path = tmp_path / "example.desktop"
    receiver["create_template"](str(path), "example", entries["example"], False)
    output = path.read_text()
    assert "Name=%VMNAME%: Example" in output
    assert "Name[fr]=%VMNAME%: Éditeur" in output
    assert "Keywords=graphics;painting;" in output
    assert "OnlyShowIn=GNOME;" in output
    assert "StartupWMClass=%VMNAME%:example" in output
    assert "Exec=airlock-launch-qube-app Application %VMNAME% qubes.StartApp+example" in output
    assert "touch /tmp" not in output
    assert "X-Airlock-Source" not in output


def test_packaged_receiver_rejects_control_characters_and_unsafe_ids(interfaces, monkeypatch):
    receiver = receiver_namespace(interfaces["desktop"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "valid.desktop:Name=Example\x1b[31m\n"
        "bad;command.desktop:Name=Example\n"
        "valid.desktop:StartupWMClass=class;command\n"
        "valid.desktop:OnlyShowIn=GNOME;$(command);\n"
    ))
    entries = receiver["get_appmenus"](None)
    assert "Name" not in entries.get("valid", {})
    assert "StartupWMClass" not in entries.get("valid", {})
    assert "OnlyShowIn" not in entries.get("valid", {})
    assert "bad;command" not in entries


def test_packaged_menus_restore_original_icon_bytes_without_rewriting_cache(
    interfaces, tmp_path,
):
    class_node = next(node for node in ast.parse(interfaces["menus"]).body
                      if isinstance(node, ast.ClassDef) and node.name == "Appmenus")
    namespace = {"filecmp": filecmp, "shutil": shutil, "os": os, "itertools": itertools}
    exec(compile(ast.Module(body=[class_node], type_ignores=[]), "packaged-menus", "exec"), namespace)
    menus = namespace["Appmenus"]()
    source, destination = tmp_path / "source", tmp_path / "icons"
    source.mkdir()
    destination.mkdir()
    original = source / "example.png"
    original.write_bytes(b"original application colour pixels")
    cached = destination / original.name
    cached.write_bytes(b"old blue tint")
    os.utime(cached, (original.stat().st_mtime + 60,) * 2)
    menus.icons_dir = lambda vm: str(destination)
    menus.whitelist_path = lambda vm: str(tmp_path / "whitelist")
    guest = SimpleNamespace(features={"menu-items": "*"}, klass="StandaloneVM")
    menus.appicons_create(guest, srcdirs=(str(source),))
    assert cached.read_bytes() == original.read_bytes()
    timestamp = cached.stat().st_mtime_ns
    menus.appicons_create(guest, srcdirs=(str(source),))
    assert cached.stat().st_mtime_ns == timestamp


def test_packaged_qvm_appmenus_uses_uuid_ids_and_wildcard_selection(interfaces):
    class_node = next(node for node in ast.parse(interfaces["menus"]).body
                      if isinstance(node, ast.ClassDef) and node.name == "Appmenus")
    namespace = {}
    exec(compile(ast.Module(body=[class_node], type_ignores=[]), "packaged-menus", "exec"), namespace)
    menus = namespace["Appmenus"]()
    first = SimpleNamespace(uuid="738a6af9-8e51-590d-afe4-790b26fbca79", features={"menu-items": "*"})
    second = SimpleNamespace(uuid="728a6af9-8e51-590d-afe4-790b26fbca79")
    assert menus.desktop_name(first, "firefox.desktop") != menus.desktop_name(second, "firefox.desktop")
    menus.get_available_filenames = lambda vm: ["/cache/firefox.desktop", "/cache/new-app.desktop"]
    namespace["os"] = os
    assert set(menus.get_whitelist(first)) == {"firefox.desktop", "new-app.desktop"}
