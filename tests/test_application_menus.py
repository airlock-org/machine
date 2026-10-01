"""Application discovery follows active profiles and trusted Qubes identities."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = load(
    "application_catalog",
    "lifecycle/misc/modules/qube-guest/scripts/application-catalog.py",
)
MENUS = load("application_menus", "lifecycle/misc/modules/sys-gui/application-menus.py")
HOME_ID = "738a6af9-8e51-590d-afe4-790b26fbca79"
WORK_ID = "728a6af9-8e51-590d-afe4-790b26fbca79"


def desktop(directory, name="firefox.desktop", **fields):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "Type": "Application",
        "Name": "Firefox",
        "Exec": "firefox %u",
        "Icon": "firefox",
        **fields,
    }
    path.write_text(
        "[Desktop Entry]\n"
        + "".join(f"{key}={value}\n" for key, value in values.items())
    )
    return path


def test_install_and_uninstall_need_no_application_specific_configuration(tmp_path):
    applications = tmp_path / "share/applications"
    desktop(applications)
    assert set(CATALOG.catalog([applications])) == {"firefox.desktop"}

    installed = desktop(
        applications,
        "new-app.desktop",
        Name="A newly installed app",
        Icon="new-icon",
        Keywords="drawing;painting;",
    )
    entry = CATALOG.catalog([applications])["new-app.desktop"]
    assert entry["Name"] == "A newly installed app"
    assert entry["Icon"] == "new-icon"
    assert entry["Keywords"] == "drawing;painting;"
    assert entry["Exec"] == "qubes-desktop-run new-app.desktop"
    installed.unlink()
    assert set(CATALOG.catalog([applications])) == {"firefox.desktop"}


def test_dbus_activated_app_does_not_need_an_exec_field(tmp_path):
    desktop(tmp_path, "org.example.App.desktop", Exec="", DBusActivatable="true")
    assert "org.example.App.desktop" in CATALOG.catalog([tmp_path])


def test_unreadable_subdirectory_aborts_snapshot(tmp_path, monkeypatch):
    def broken_walk(*args, **kwargs):
        kwargs["onerror"](PermissionError("unreadable directory"))
        return iter(())

    monkeypatch.setattr(CATALOG.os, "walk", broken_walk)
    with pytest.raises(PermissionError):
        CATALOG.catalog([tmp_path])


def test_profile_switch_excludes_old_generations_and_invalidates_icon_cache(tmp_path):
    old, new = tmp_path / "generation-1", tmp_path / "generation-2"
    desktop(old / "share/applications")
    desktop(old / "share/applications", "old-only.desktop")
    desktop(new / "share/applications")
    desktop(new / "share/applications", "new-only.desktop")
    profile = tmp_path / "active-profile"
    profile.symlink_to(old)
    dirs = [profile / "share/applications"]
    before = CATALOG.catalog(dirs)
    profile.unlink()
    profile.symlink_to(new)
    after = CATALOG.catalog(dirs)
    assert set(after) == {"firefox.desktop", "new-only.desktop"}
    assert (
        before["firefox.desktop"]["X-Airlock-Source"]
        != after["firefox.desktop"]["X-Airlock-Source"]
    )
    # The retained old generation still exists, but is not an installed app source.
    assert (old / "share/applications/old-only.desktop").exists()


@pytest.mark.parametrize(
    "fields", [{"Hidden": "true"}, {"NoDisplay": "true"}, {"TryExec": "not-installed"}]
)
def test_user_override_masks_system_desktop_id_even_if_not_visible(tmp_path, fields):
    personal, system = tmp_path / "user", tmp_path / "system"
    desktop(personal, **fields)
    desktop(system)
    assert CATALOG.catalog([personal, system], environ={"PATH": ""}) == {}


def test_installed_tryexec_is_exposed_and_localized_metadata_is_preserved(tmp_path):
    desktop(
        tmp_path,
        TryExec="python3",
        **{
            "Name[fr]": "Navigateur",
            "Keywords[fr]": "web;internet;",
            "OnlyShowIn": "GNOME;",
            "StartupWMClass": "firefox",
        },
    )
    item = CATALOG.catalog([tmp_path])["firefox.desktop"]
    assert item["Name[fr]"] == "Navigateur"
    assert item["Keywords[fr]"] == "web;internet;"
    assert item["OnlyShowIn"] == "GNOME;"
    assert item["StartupWMClass"] == "firefox"


def test_nested_desktop_ids_and_profile_directory_symlinks(tmp_path):
    package, profile = tmp_path / "package", tmp_path / "profile"
    desktop(package / "wine/Programs", "my-app.desktop", Name="My app")
    profile.mkdir()
    (profile / "wine").symlink_to(package / "wine")
    (package / "wine/loop").symlink_to(package / "wine")
    assert set(CATALOG.catalog([profile])) == {"wine-Programs-my-app.desktop"}


def test_partial_filesystem_scan_is_not_published_as_uninstall(
    tmp_path, monkeypatch, capsys
):
    applications = tmp_path / "applications"
    desktop(applications)
    broken = applications / "broken.desktop"
    broken.symlink_to(tmp_path / "missing.desktop")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        CATALOG.main()
    assert capsys.readouterr().out == ""


def test_profiles_are_discovered_in_xdg_order_and_relative_paths_are_ignored():
    dirs = CATALOG.application_dirs(
        {
            "HOME": "/home/user",
            "XDG_DATA_HOME": "/custom",
            "XDG_DATA_DIRS": "/user-profile/share:/system/share:relative:/user-profile/share",
        }
    )
    assert dirs == [
        Path("/custom/applications"),
        Path("/user-profile/share/applications"),
        Path("/system/share/applications"),
    ]


def test_identical_apps_in_two_vms_have_distinct_stable_favorites():
    first = MENUS.desktop_name(HOME_ID, "firefox.desktop")
    second = MENUS.desktop_name(WORK_ID, "firefox.desktop")
    assert first != second
    assert HOME_ID in first and WORK_ID in second
    assert MENUS.desktop_name(HOME_ID, "firefox.desktop") == first
    assert MENUS.desktop_name(HOME_ID, "firefox.desktop", disposable=True) != first
    with pytest.raises(ValueError):
        MENUS.desktop_name("invalid", "firefox.desktop")


def vm(name="usr-home", identity=HOME_ID, running=True, **kwargs):
    return SimpleNamespace(
        name=name,
        uuid=identity,
        running=running,
        label="#3465a4",
        **{"menu-items": "*"},
        **kwargs,
    )


class FakeBackend:
    def __init__(self, items=(), cached=()):
        self.items = {item.uuid: vars(item) for item in items}
        self.cached = cached
        self.actions = []

    def inventory(self):
        return self.items

    def cached_names(self):
        return self.cached

    def remove(self, name):
        self.actions.append(("remove", name))

    def rename(self, old, new):
        self.actions.append(("rename", old, new))

    def regenerate(self, name):
        self.actions.append(("regenerate", name))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return MENUS.state_dir()


def test_stopped_vm_is_searchable_without_background_start(state):
    backend = FakeBackend([vm(running=False), vm("usr-work", WORK_ID)])
    refreshed = []
    MENUS.reconcile(backend, refreshed.append)
    assert [item["name"] for item in refreshed] == ["usr-work"]
    assert ("remove", "usr-home") not in backend.actions
    assert MENUS.read_json(state / "inventory.json", {})[HOME_ID]["name"] == "usr-home"


def test_rename_regenerates_cached_stopped_vm_and_removal_withdraws_entries(state):
    MENUS.write_json(
        state / "inventory.json",
        {HOME_ID: vars(vm()), WORK_ID: vars(vm("usr-work", WORK_ID))},
    )
    backend = FakeBackend(
        [vm("usr-personal", HOME_ID, running=False)],
        cached=["usr-home", "usr-work", "retired-vm"],
    )
    MENUS.reconcile(backend, lambda _: pytest.fail("must not start stopped VM"))
    assert backend.actions == [
        ("remove", "usr-work"),
        ("remove", "retired-vm"),
        ("rename", "usr-home", "usr-personal"),
        ("regenerate", "usr-personal"),
    ]
    assert set(MENUS.read_json(state / "inventory.json", {})) == {HOME_ID}


def test_failed_inventory_preserves_catalog_and_membership(state):
    previous = {HOME_ID: vars(vm(running=False))}
    MENUS.write_json(state / "inventory.json", previous)
    backend = FakeBackend()
    backend.inventory = lambda: (_ for _ in ()).throw(
        ConnectionError("qubesd unavailable")
    )
    with pytest.raises(ConnectionError):
        MENUS.reconcile(
            backend, lambda _: pytest.fail("no refresh after failed inventory")
        )
    assert backend.actions == []
    assert MENUS.read_json(state / "inventory.json", {}) == previous


def test_recreated_vm_name_does_not_reuse_original_identity(state):
    MENUS.write_json(state / "inventory.json", {HOME_ID: vars(vm())})
    backend = FakeBackend([vm(identity=WORK_ID)])
    refreshed = []
    MENUS.reconcile(backend, refreshed.append)
    assert backend.actions[0] == ("remove", "usr-home")
    assert refreshed[0]["uuid"] == WORK_ID


@pytest.mark.parametrize(
    "change", ["other-gui", "retired", "internal", "ephemeral", "self"]
)
def test_membership_comes_from_qubes_and_excludes_unassigned_retired_vms(change):
    guest = SimpleNamespace(
        name="usr-work",
        uuid=WORK_ID,
        guivm="sys-gui",
        tags=set(),
        features={},
        klass="StandaloneVM",
        label=SimpleNamespace(color="#0000ff"),
        is_running=lambda: False,
    )
    backend = object.__new__(MENUS.Backend)
    backend.app = SimpleNamespace(local_name="sys-gui")
    assert backend.description(guest)["uuid"] == WORK_ID
    if change == "other-gui":
        guest.guivm = "other-gui"
    elif change == "retired":
        guest.tags.add("airlock-retired")
    elif change == "internal":
        guest.features["internal"] = True
    elif change == "ephemeral":
        guest.klass, guest.auto_cleanup = "DispVM", True
    elif change == "self":
        guest.name = "sys-gui"
    assert backend.description(guest) is None


@pytest.fixture
def sync_backend(state, tmp_path, monkeypatch):
    backend = object.__new__(MENUS.Backend)
    guest = SimpleNamespace(name="usr-home", uuid=HOME_ID)
    backend.app = SimpleNamespace(domains={"usr-home": guest})
    backend.basedir = tmp_path / "catalogs"
    calls = []
    guest.run_service = lambda service, **kw: calls.append((service, kw))
    guest.run_service_for_stdio = lambda service, **kw: calls.append((service, kw))
    backend.description = lambda _vm: vars(vm())
    backend.receive = SimpleNamespace(
        get_appmenus=lambda target: (target.run_service("qubes.GetAppmenus"), {})[1],
        process_appmenus_templates=lambda menus, target, items: calls.append(
            ("publish", items)
        ),
    )
    backend.menus = SimpleNamespace(
        appmenus_update=lambda guest: calls.append(("regenerate", guest.name))
    )
    monkeypatch.setattr(MENUS, "Backend", lambda: backend)
    return backend, calls


def test_successful_empty_catalog_removes_old_apps_without_starting_guest(sync_backend):
    backend, calls = sync_backend
    backend.sync("usr-home", HOME_ID)
    assert calls == [
        ("qubes.GetAppmenus", {"autostart": False}),
        ("publish", {}),
        ("regenerate", "usr-home"),
    ]


def test_failed_catalog_retrieval_does_not_publish_partial_snapshot(sync_backend):
    backend, calls = sync_backend
    backend.receive.get_appmenus = lambda _: (_ for _ in ()).throw(
        ConnectionError("qrexec disconnected")
    )
    with pytest.raises(ConnectionError):
        backend.sync("usr-home", HOME_ID)
    assert calls == []


def test_wrong_uuid_and_shutdown_races_do_not_publish(sync_backend):
    backend, calls = sync_backend
    with pytest.raises(ValueError, match="identity changed"):
        backend.sync("usr-home", WORK_ID)
    assert calls == []
    descriptions = iter([vars(vm()), vars(vm(running=False))])
    backend.description = lambda _: next(descriptions)
    with pytest.raises(ValueError, match="VM changed"):
        backend.sync("usr-home", HOME_ID)
    assert calls == [("qubes.GetAppmenus", {"autostart": False})]


def test_unchanged_catalog_skips_icons_but_force_and_cache_loss_refresh(sync_backend):
    backend, calls = sync_backend
    backend.sync("usr-home")
    (backend.basedir / "usr-home/apps.templates").mkdir(parents=True)
    calls.clear()
    backend.sync("usr-home")
    assert calls == [("qubes.GetAppmenus", {"autostart": False})]
    calls.clear()
    backend.sync("usr-home", force=True)
    assert ("publish", {}) in calls
    (backend.basedir / "usr-home/apps.templates").rmdir()
    calls.clear()
    backend.sync("usr-home")
    assert ("publish", {}) in calls


def test_worker_timeout_releases_locks_and_terminates_children(tmp_path, monkeypatch):
    worker = tmp_path / "blocked-worker.py"
    child_pid = tmp_path / "child.pid"
    worker.write_text(f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open({str(child_pid)!r}, 'w') as stream:
    stream.write(str(child.pid))
time.sleep(60)
""")
    monkeypatch.setattr(MENUS, "__file__", str(worker))
    MENUS.refresh_worker(vars(vm()), timeout=0.5)
    assert child_pid.exists()
    pid = int(child_pid.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
