"""Airlock's systemd-boot menu exposes current, recovery and old generations."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def boot_menu():
    source = (
        Path(__file__).resolve().parents[1]
        / "lifecycle/misc/tools/scripts/boot-menu.py"
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                          "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"))
    spec = importlib.util.spec_from_file_location("airlock_boot_menu", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_entry(esp, store, name, generation, role=None, title="NixOS"):
    system = store / name
    if role:
        marker = system / "etc/airlock/boot-role"
        marker.parent.mkdir(parents=True)
        marker.write_text(role + "\n")
        (marker.parent / "boot-source.json").write_text(json.dumps({
            "revision": f"{generation:040x}", "timestamp": 1790778600 + generation * 60}))
    ordinary = esp / "loader/entries" / f"nixos-{name}.conf"
    ordinary.write_text(
        f"title {title}\n"
        f"version Generation {generation} test\n"
        f"options init=/nix/store/{name}/init console=tty0\n"
        "sort-key nixos\n"
    )
    xen = esp / "loader/entries" / f"xen-{name}.conf"
    xen.write_text(
        f"title {title} (with Xen Hypervisor)\n"
        f"version Generation {generation} test\n"
        f"efi /EFI/nixos/xen-{name}.efi\n"
        "sort-key nixos\n"
    )
    return xen


def menu_fixture(tmp_path):
    esp = tmp_path / "esp"
    store = tmp_path / "store"
    entries = esp / "loader/entries"
    entries.mkdir(parents=True)
    store.mkdir()
    loader = esp / "loader/loader.conf"
    loader.write_text("timeout 10\npreferred xen-base12\ndefault xen-base12\n")
    add_entry(esp, store, "base12", 12)
    current = add_entry(esp, store, "lab12", 12, "normal", "NixOS (usb-lab)")
    recovery = add_entry(
        esp, store, "recovery12", 12, "recovery", "NixOS (usb-text-recovery)"
    )
    previous = add_entry(esp, store, "lab11", 11, "normal")
    add_entry(esp, store, "lab10a", 10, "normal", "NixOS (usb-lab)")
    add_entry(esp, store, "lab10b", 10, "normal")
    add_entry(esp, store, "recovery11", 11, "recovery")
    add_entry(esp, store, "gui12", 12, None, "NixOS (usb-gui-lab)")
    other = entries / "other.conf"
    other.write_text("title Other OS\nefi /EFI/other.efi\n")
    return esp, store, current, recovery, previous, other


def test_menu_orders_all_normal_before_recovery(boot_menu, tmp_path):
    esp, store, current, recovery, previous, other = menu_fixture(tmp_path)
    boot_menu.configure(esp, store)
    entries = [entry for entry in (esp / "loader/entries").glob("*.conf") if entry != other]
    ordered = sorted(entries, key=lambda entry: boot_menu.fields(entry)["sort-key"])
    assert [(entry.generation, entry.role) for entry in sorted(
        boot_menu.classified_entries(esp / "loader/entries", store),
        key=lambda entry: boot_menu.fields(entry.path)["sort-key"])] == [
        (12, "normal"), (11, "normal"), (10, "normal"), (12, "recovery"), (11, "recovery")]
    assert ordered[:2] == [current, previous]
    assert boot_menu.fields(current)["title"] == "Airlock (2026-09-30_14:42-0000000)"
    assert boot_menu.fields(recovery)["title"] == "Airlock (recovery, 2026-09-30_14:42-0000000)"
    assert boot_menu.fields(current)["version"] == "12"
    assert other.is_file()
    loader = boot_menu.fields(esp / "loader/loader.conf")
    assert loader["default"] == loader["preferred"] == current.name
    before = {path: path.read_bytes() for path in esp.rglob("*.conf")}
    boot_menu.configure(esp, store)
    assert {path: path.read_bytes() for path in esp.rglob("*.conf")} == before


def test_menu_refuses_to_mutate_without_recovery(boot_menu, tmp_path):
    esp, store, current, recovery, _, _ = menu_fixture(tmp_path)
    for marker in store.glob("recovery*/etc/airlock/boot-role"):
        marker.unlink()
    before = {path: path.read_text() for path in esp.rglob("*.conf")}

    with pytest.raises(ValueError, match="no Xen Airlock recovery"):
        boot_menu.configure(esp, store)

    assert {path: path.read_text() for path in esp.rglob("*.conf")} == before


def test_menu_pins_recovery_as_a_system_profile(boot_menu, tmp_path, monkeypatch):
    esp, store, _, _, _, _ = menu_fixture(tmp_path)
    profile = tmp_path / "profiles/system-profiles/airlock-recovery"
    calls = []
    monkeypatch.setattr(
        boot_menu.subprocess,
        "run",
        lambda command, check: calls.append((command, check)),
    )

    boot_menu.configure(
        esp, store, recovery_profile=profile, nix_env="/nix/store/nix/bin/nix-env"
    )

    assert profile.parent.is_dir()
    assert calls == [
        (
            [
                "/nix/store/nix/bin/nix-env",
                "--profile",
                str(profile),
                "--set",
                str(store / "recovery12"),
            ],
            True,
        )
    ]


def test_selected_older_generation_does_not_change_newest_default(boot_menu, tmp_path):
    esp, store, current, _, previous, _ = menu_fixture(tmp_path)
    loader = esp / "loader/loader.conf"
    loader.write_text(f"timeout 10\ndefault {previous.stem}\n")

    boot_menu.configure(esp, store)

    assert boot_menu.fields(loader)["default"] == current.name


def test_retained_recovery_is_usable_without_its_store_view(boot_menu, tmp_path):
    esp, store, current, _, _, _ = menu_fixture(tmp_path)
    import shutil
    for system in store.glob('recovery*'):
        shutil.rmtree(system)
    retained = esp / 'loader/entries/airlock-xen-recovery.conf'
    text = ('title Airlock (recovery)\nversion Generation 1 retained\n'
            'efi /EFI/airlock/recovery.efi\n'
            '# airlock-system /nix/store/recovery1\n# airlock-role recovery\n')
    retained.write_text(text)
    boot_menu.configure(esp, store, recovery_profile=tmp_path / 'recovery-profile')
    assert retained.read_text() == text
    assert not (tmp_path / 'recovery-profile').exists()
    assert boot_menu.fields(esp / 'loader/loader.conf')['default'] == current.name


def test_regenerated_native_duplicates_keep_sealed_choices_and_default(boot_menu, tmp_path):
    esp, store, current, recovery, previous, _ = menu_fixture(tmp_path)
    loader = esp / 'loader/loader.conf'
    loader.write_text(f'default {previous.stem}\npreferred {previous.stem}\n')
    sealed = {}
    for native, generation, role in ((current, 12, 'normal'),
                                     (previous, 11, 'normal'),
                                     (recovery, 12, 'recovery')):
        target = native.with_name('airlock-' + native.name)
        system = boot_menu.system_for_xen_entry(native, store)
        title = 'Airlock (recovery)' if role == 'recovery' else 'Airlock'
        text = (f'title {title}\nversion Generation {generation}\n'
                f'efi /EFI/airlock/{target.stem}.efi\n'
                f'# airlock-system /nix/store/{system.name}\n# airlock-role {role}\n')
        target.write_text(text)
        sealed[target] = text

    boot_menu.configure(esp, store)

    assert not any(path.exists() for path in (current, previous, recovery))
    assert all(path.read_text() == text for path, text in sealed.items())
    assert boot_menu.fields(loader)['default'] == 'airlock-' + current.name
    assert boot_menu.fields(loader)['preferred'] == 'airlock-' + current.name


def test_numeric_order_with_identical_source_labels(boot_menu, tmp_path):
    esp, store, _, _, _, _ = menu_fixture(tmp_path)
    older = add_entry(esp, store, "two", 2, "normal")
    newer = esp / "loader/entries/xen-lab10a.conf"
    same = json.dumps({"revision": "abcdef0" + "1" * 33, "timestamp": 1790778600})
    for marker in store.glob("*/etc/airlock/boot-source.json"):
        marker.write_text(same)
    boot_menu.configure(esp, store)
    assert boot_menu.fields(older)["title"] == boot_menu.fields(newer)["title"]
    ordered = sorted(boot_menu.classified_entries(esp / "loader/entries", store),
                     key=lambda entry: boot_menu.fields(entry.path)["sort-key"])
    assert [entry.generation for entry in ordered if entry.role == "normal"] == [12, 11, 10, 2]


def test_invalid_source_refuses_mutation(boot_menu, tmp_path):
    esp, store, *_ = menu_fixture(tmp_path)
    (store / "lab12/etc/airlock/boot-source.json").write_text(
        json.dumps({"revision": "dirty", "timestamp": 1790778600}))
    before = {path: path.read_bytes() for path in esp.rglob("*.conf")}
    with pytest.raises(ValueError, match="committed revision"):
        boot_menu.configure(esp, store)
    assert {path: path.read_bytes() for path in esp.rglob("*.conf")} == before
