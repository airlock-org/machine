"""The assistant name and one-shot data-preserving transition in real Nix configuration."""

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_normal_and_transition_releases_keep_the_same_profile_and_scoped_copy_intent():
    expression = f"""
      let
        f = builtins.getFlake "{ROOT}";
        normal = f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration;
        transition = f.nixosConfigurations.machine-assistant-rename.config.specialisation.usb-lab.configuration;
        host = c: {{
          profile = c.airlock.machineDeployment.profile;
          retired = c.airlock.machineDeployment.retireDomains;
          copies = c.airlock.machineDeployment.privateVolumeCopies;
          domains = builtins.attrNames c.airlock.qubes.domains;
          callers = c.airlock.devDom0Terminal.callers;
        }};
      in {{
        normal = host normal;
        transition = host transition;
        package = f.packages.x86_64-linux.airlock-machine-release-usb-lab-assistant-rename.name;
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    for config in [data["normal"], data["transition"]]:
        assert config["profile"] == "usb-lab"
        assert "usr-assistant" in config["domains"]
        assert "sys-agent" not in config["domains"]
        assert config["callers"] == ["usr-assistant"]
    assert data["normal"]["retired"] == []
    assert data["normal"]["copies"] == {}
    assert data["transition"]["retired"] == ["sys-agent"]
    assert data["transition"]["copies"] == {"usr-assistant": "sys-agent"}
    assert data["package"]
