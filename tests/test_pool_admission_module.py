from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.nix
def test_pool_admission_uses_narrow_root_receiver():
    expression = f"""
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
        incrementalHandler = builtins.readFile image.environment.etc."qubes-rpc/airlock.Pool.AdmitIncremental".source;
        receivers = map (entry: builtins.readFile entry.command) rule.commands;
        options = map (entry: entry.options) rule.commands;
        policy = builtins.readFile "${{builtins.head (builtins.filter (p: builtins.pathExists "${{p}}/etc/qubes/policy.d/42-airlock-pool-admission.policy") image.virtualisation.qubes.packages)}}/etc/qubes/policy.d/42-airlock-pool-admission.policy";
        sudoUsers = rule.users;
        sudoOptions = (builtins.head rule.commands).options;
        matchingRuleCount = builtins.length rules;
        targets = image.airlock.poolAdmission.targets;
      }}
    """
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
        assert (
            f"airlock.Pool.AdmitIncremental +{target} sys-dev dom0 allow"
            in report["policy"]
        )
    assert "airlock.Pool.AdmitIncremental * @anyvm @anyvm deny" in report["policy"]
    assert "/run/wrappers/bin/sudo -n" in report["incrementalHandler"]
    assert len(report["receivers"]) == 2
    assert report["options"] == [["NOPASSWD"], ["NOPASSWD"]]
    assert "receive-incremental" in report["receivers"][1]
    for receiver in report["receivers"]:
        assert "root receiver rejected caller" in receiver
        assert "root receiver rejected target" in receiver
