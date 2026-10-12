"""Runtime bootstrap PCI selection using fake Qubes commands."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "domain,route,exclusions,expected",
    [
        ("usr-assistant", "00_03.0", "", ("00_03.0", "00_04.0")),
        ("usr-assistant", "00_03.0", "00_04.0", ("00_03.0",)),
        ("sys-net", "", "", ("00_03.0", "00_04.0")),
        ("sys-net", "00_03.0", "", ("00_04.0",)),
    ],
)
def test_bootstrap_network_pci_selection(tmp_path, domain, route, exclusions, expected):
    from machine_pychecks import check_setup_config_auto_sys_net_pci

    check_setup_config_auto_sys_net_pci(
        ROOT,
        tmp_path,
        domain_name=domain,
        default_route=route,
        exclusions=exclusions,
        expected_devices=expected,
    )
