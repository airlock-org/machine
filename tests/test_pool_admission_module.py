from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_pool_admission_uses_narrow_root_receiver():
    expression = f'''
      let
        flake = builtins.getFlake "{ROOT}";
        image = flake.nixosConfigurations.vm-pool-admission-test.config;
        rules = builtins.filter (rule:
          builtins.any (entry:
            builtins.match ".*airlock-pool-admit-root.*" entry.command != null
          ) rule.commands
        ) image.security.sudo.extraRules;
        rule = builtins.head rules;
        command = (builtins.head rule.commands).command;
      in {{
        handler = builtins.readFile image.environment.etc."qubes-rpc/airlock.Pool.Admit".source;
        rootReceiver = builtins.readFile command;
        sudoUsers = rule.users;
        sudoOptions = (builtins.head rule.commands).options;
        matchingRuleCount = builtins.length rules;
        targets = image.airlock.poolAdmission.targets;
      }}
    '''
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    log_dir = Path(os.environ["AIRLOCK_LOG_DIR"]) / "pool-admission-module"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "report.json").write_text(result.stdout)
    report = json.loads(result.stdout)

    assert report["matchingRuleCount"] == 1
    assert report["sudoUsers"] == ["airlock"]
    assert report["sudoOptions"] == ["NOPASSWD"]
    assert "/run/wrappers/bin/sudo -n" in report["handler"]
    assert "airlock-pool-admit-root" in report["handler"]
    assert 'approved_by="$2"' in report["rootReceiver"]
    assert "root receiver rejected caller" in report["rootReceiver"]
    assert "root receiver rejected target" in report["rootReceiver"]
    for target in report["targets"]:
        assert target in report["rootReceiver"]
