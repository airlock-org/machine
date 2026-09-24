"""Network failure handling without a running VM or Internet access."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "lifecycle/misc/tools/scripts"


def executable(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize(
    "failure,message",
    [
        ("route", "No network route"),
        ("dns", "DNS is not working"),
        ("https", "Cannot reach Tailscale"),
        ("login", "login timed out"),
        ("", "Starting Tailscale login"),
    ],
)
def test_enrollment_reports_network_failures_without_hanging(
    tmp_path, failure, message
):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls"
    executable(
        binaries / "ip", '[ "$FAILURE" = route ] || echo "default via 10.137.0.1"\n'
    )
    executable(binaries / "getent", '[ "$FAILURE" != dns ]\n')
    executable(binaries / "curl", '[ "$FAILURE" != https ]\n')
    executable(binaries / "tailscale", 'echo "$*" > "$CALLS"\n')
    executable(
        binaries / "timeout",
        """
case "$*" in
  *"120s tailscale"*)
    [ "$FAILURE" != login ] || exit 124
    exec tailscale up --ssh ;;
  *"5s getent"*) exec getent ahostsv4 controlplane.tailscale.com ;;
  *) exit 99 ;;
esac
""",
    )
    result = subprocess.run(
        ["bash", str(SCRIPTS / "dev-tailscale-up.sh")],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "FAILURE": failure,
            "CALLS": str(calls),
            "QREXEC_REMOTE_DOMAIN": "sys-gui",
            "QREXEC_SERVICE_ARGUMENT": "",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert message in result.stdout + result.stderr
    assert (result.returncode == 0) == (failure == "")
    assert calls.exists() == (failure == "")


@pytest.mark.parametrize("healthy", [True, False])
def test_provider_readiness_requires_firewall_even_when_other_units_are_active(
    tmp_path, healthy
):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("qvm-start", "qvm-check", "qvm-ls", "supervisor"):
        executable(binaries / name, "exit 0\n")
    executable(
        binaries / "qrexec-client",
        """
case "$3" in
  user:*) echo /nix/store/expected ;;
  root:*) exec bash -c "${3#root:}" ;;
  *) exit 1 ;;
esac
""",
    )
    # Like real is-active, multiple units could return success despite a dead
    # firewall. The probe must check units individually.
    executable(
        binaries / "systemctl",
        """
[ "$#" -gt 3 ] && exit 0
[ "$HEALTHY" = yes ] || [ "$3" != qubes-firewall.service ]
""",
    )
    topology = tmp_path / "topology.json"
    topology.write_text(
        json.dumps(
            {
                "domains": {
                    "sys-net": {
                        "prefs": {"provides_network": True},
                        "backend": {"system": {"source": "/nix/store/expected"}},
                    }
                }
            }
        )
    )
    result = subprocess.run(
        ["bash", str(SCRIPTS / "start-qube-ready.sh"), "sys-net", str(topology)],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "HEALTHY": "yes" if healthy else "no",
            "AIRLOCK_BOOT_SUPERVISOR": str(binaries / "supervisor"),
            "AIRLOCK_QUBE_READY_TIMEOUT": "1",
            "AIRLOCK_BOOT_ATTEMPT_LOG_DIR": str(tmp_path / "logs"),
        },
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert (result.returncode == 0) == healthy
    if not healthy:
        assert (
            "Network provider unhealthy: qubes-firewall.service"
            in (tmp_path / "logs/start-sys-net.log").read_text()
        )


@pytest.mark.nix
def test_network_configuration_has_recovery_firmware_and_one_dns_owner():
    result = subprocess.run(
        [
            "nix",
            "eval",
            "--impure",
            "--json",
            "--expr",
            f"""
        let f = builtins.getFlake "path:{ROOT}";
            net = f.nixosConfigurations.airlock-domain-sys-net.config;
            fw = f.nixosConfigurations.airlock-domain-sys-firewall.config;
        in {{
          firmware = net.hardware.enableRedistributableFirmware;
          netResolver = net.networking.resolvconf.enable;
          fwResolver = fw.networking.resolvconf.enable;
          fwCommands = fw.networking.localCommands;
          netRestart = net.systemd.services.qubes-firewall.serviceConfig.Restart;
          fwRestart = fw.systemd.services.qubes-firewall.serviceConfig.Restart;
        }}
        """,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    assert config["firmware"] is True
    assert config["netResolver"] is True
    assert config["fwResolver"] is False
    assert "resolvconf" not in config["fwCommands"]
    assert config["netRestart"] == config["fwRestart"] == "on-failure"
