import importlib.util
import io
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sys_ctrl_guest_deploy", ROOT / "lifecycle/runtime/tools/sys-ctrl-guest-deploy.py"
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


def test_controller_rejects_unapproved_caller_before_reading_input(monkeypatch):
    monkeypatch.setenv("QREXEC_REMOTE_DOMAIN", "usr-browser")
    with pytest.raises(ValueError, match="only sys-dev"):
        DEPLOY.serve()


@pytest.mark.parametrize("target", ["sys-config-vault", "dom0", "../usr-home"])
def test_submission_rejects_non_guest_targets_before_bundling(target, tmp_path):
    with pytest.raises(ValueError, match="unsupported guest"):
        DEPLOY.request("apply", target, tmp_path)


def test_controller_rejects_unversioned_request_before_import(monkeypatch):
    monkeypatch.setenv("QREXEC_REMOTE_DOMAIN", "sys-dev")
    monkeypatch.setattr(DEPLOY.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{}\n')))
    with pytest.raises(ValueError, match="unsupported request schema"):
        DEPLOY.serve()


@pytest.mark.nix
def test_controller_and_dom0_policy_wiring():
    expression = f'''
      let f = builtins.getFlake "{ROOT}";
          ctrl = f.nixosConfigurations.airlock-domain-sys-ctrl.config;
          host = f.nixosConfigurations.machine.config.specialisation.usb-lab.configuration;
      in {{
        controllerService = toString ctrl.environment.etc."qubes-rpc/airlock.Ctrl.GuestDeploy".source;
        planService = toString host.environment.etc."qubes-rpc/airlock.Ctrl.GuestPlan".source;
        applyService = toString host.environment.etc."qubes-rpc/airlock.Ctrl.GuestApply".source;
        machinePlan = toString host.environment.etc."qubes-rpc/airlock.Ctrl.MachinePlan".source;
        machineApply = toString host.environment.etc."qubes-rpc/airlock.Ctrl.MachineApply".source;
        admissionCaller = host.airlock.poolAdmission.caller;
        builderService = ctrl.systemd.services.airlock-sys-ctrl-build-store.description;
        bootDiagnostics = host.airlock.bootDiagnostics.enable;
      }}
    '''
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert report["admissionCaller"] == "sys-ctrl"
    assert report["bootDiagnostics"] is True
    assert "sys-ctrl" in report["builderService"]
    assert all(report[key].startswith("/nix/store/") for key in
               ("controllerService", "planService", "applyService", "machinePlan", "machineApply"))


@pytest.mark.parametrize("stage", ["inventory", "planning"])
def test_machine_response_preserves_dom0_failure(stage):
    with pytest.raises(ValueError, match="specific admission failure"):
        DEPLOY.machine_response(json.dumps({"schema": DEPLOY.MACHINE_SCHEMA,
                                          "error": "specific admission failure"}), stage)
    with pytest.raises(ValueError, match="closed the machine"):
        DEPLOY.machine_response(b"", stage)


def test_machine_metadata_queries_shared_paths_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(DEPLOY, 'BUILDER_STORE', tmp_path)
    topology = {'domains': {}}
    for vm in ['one', 'two']:
        closure = tmp_path / 'nix/store' / (vm + '-closure')
        closure.mkdir(parents=True)
        (closure / 'store-paths').write_text('/nix/store/shared\n/nix/store/' + vm + '\n')
        topology['domains'][vm] = {'backend': {'type': 'nix-closure',
            'system': {'source': '/nix/store/' + vm}, 'store': {'source': '/nix/store/' + vm + '-closure'}}}
    seen = []
    def output(*args):
        assert args[:3] == ('airlock-config-nix', 'path-info', '--json')
        seen.extend(args[3:])
        return json.dumps({path: {'narHash': 'hash'} for path in args[3:]})
    monkeypatch.setattr(DEPLOY, 'output', output)
    result = DEPLOY.machine_metadata(topology)
    assert len(seen) == len(set(seen)) == 3
    assert set(result) == set(seen)


def test_cached_build_requires_matching_source_and_retained_output(tmp_path, monkeypatch):
    revision, lock_hash = 'a' * 40, 'b' * 64
    release = '/nix/store/' + 'c' * 32 + '-release'
    monkeypatch.setattr(DEPLOY, 'BUILDER_STORE', tmp_path / 'store')
    monkeypatch.setattr(DEPLOY, 'BUILD_RECORDS', tmp_path / 'records')
    monkeypatch.setattr(DEPLOY, 'identity', lambda _: (revision, lock_hash))
    physical = DEPLOY.BUILDER_STORE / release.lstrip('/')
    physical.mkdir(parents=True)
    (physical / 'release.json').write_text(json.dumps({'schema': 'airlock.machine-release.v1',
        'profile': 'usb-lab', 'revision': revision, 'lockHash': lock_hash}))
    directory = DEPLOY.BUILD_RECORDS / revision
    directory.mkdir(parents=True)
    (directory / 'release').symlink_to(release)
    (directory / 'build.json').write_text(json.dumps({'release': release, 'revision': revision, 'lock_hash': lock_hash}))
    monkeypatch.setattr(DEPLOY, 'output', lambda *_: pytest.fail('release reevaluated'))
    assert DEPLOY.build_machine_release(revision, lock_hash) == release
    assert json.loads((directory / 'build.json').read_text())['build_reused'] is True
    def rebuilding(*_):
        raise RuntimeError('build required')
    monkeypatch.setattr(DEPLOY, 'output', rebuilding)
    (directory / 'release').unlink()
    with pytest.raises(RuntimeError, match='build required'):
        DEPLOY.build_machine_release(revision, lock_hash)


def test_planned_apply_skips_all_inventories_and_transfers(tmp_path, monkeypatch):
    revision, lock_hash = 'a' * 40, 'b' * 64
    release = '/nix/store/' + 'c' * 32 + '-release'
    transport = '/nix/store/' + 'd' * 32 + '-transport'
    monkeypatch.setattr(DEPLOY, 'BUILDER_STORE', tmp_path / 'store')
    monkeypatch.setattr(DEPLOY, 'STATE', tmp_path / 'controller')
    physical = DEPLOY.BUILDER_STORE / release.lstrip('/')
    physical.mkdir(parents=True)
    for name in ['topology.json', 'root-artifacts.json']:
        (physical / name).write_text('{}')
    (physical / 'transport').symlink_to(transport)
    DEPLOY.record(DEPLOY.STATE / 'candidates' / revision / 'plan.json',
                  {'prepared': 'token', 'source_release': transport})
    calls = []
    monkeypatch.setattr(DEPLOY, 'send_prepared', lambda *args: calls.append(args))
    monkeypatch.setattr(DEPLOY, 'machine_metadata', lambda *_: pytest.fail('inventory queried'))
    DEPLOY.send_machine_release('apply', release, revision, lock_hash)
    assert len(calls) == 1
    assert calls[0][:3] == (release, revision, lock_hash)


@pytest.mark.parametrize("change", [None, "domain", "activation", "token", "release"])
def test_machine_prepares_candidate_before_new_guest_payload(tmp_path, monkeypatch, capsys, change):
    revision, lock_hash = "a" * 40, "b" * 64
    release, transport = "/nix/store/release", "/nix/store/transport"
    monkeypatch.setattr(DEPLOY, "BUILDER_STORE", tmp_path / "store")
    monkeypatch.setattr(DEPLOY, "STATE", tmp_path / "state")
    monkeypatch.setattr(DEPLOY, "BUILD_RECORDS", tmp_path / "records")
    physical = DEPLOY.BUILDER_STORE / release.lstrip("/")
    physical.mkdir(parents=True)
    (physical / "transport").symlink_to(transport)
    (physical / "topology.json").write_text(json.dumps({"domains": {"usr-new": {}}}))
    (physical / "root-artifacts.json").write_text('{}')
    DEPLOY.record(DEPLOY.BUILD_RECORDS / revision / "build.json", {"build_seconds": 1})
    authorization = {"schema": "airlock.machine-admission.v1", "release": transport,
                     "admission": "c" * 64, "domains": ["usr-new"], "activated": False,
                     "paths_transferred": 3, "nar_bytes": 7, "compressed_bytes": 5}
    if change == "domain":
        authorization["domains"] = ["usr-other"]
    elif change == "activation":
        authorization["activated"] = True
    elif change == "token":
        authorization["admission"] = "../invalid"
    elif change == "release":
        authorization["release"] = "other"
    calls = []
    def transport_request(action, *_args):
        calls.append(action)
        if action == "prepare":
            return authorization
        return {"paths_transferred": 0, "nar_bytes": 0, "compressed_bytes": 1}
    monkeypatch.setattr(DEPLOY, "send_machine_transport", transport_request)
    monkeypatch.setattr(DEPLOY, "machine_metadata", lambda *_: {})
    def admit(*args):
        assert calls == ["prepare"]
        assert args[-1] == "c" * 64
        calls.append("guest-payload")
        return {}
    monkeypatch.setattr(DEPLOY, "admit_machine_guests", admit)
    if change:
        with pytest.raises(ValueError, match="authorization"):
            DEPLOY.send_machine_release("plan", release, revision, lock_hash)
        assert calls == ["prepare"]
    else:
        DEPLOY.send_machine_release("plan", release, revision, lock_hash)
        assert calls == ["prepare", "guest-payload", "plan"]
        response = json.loads(capsys.readouterr().out)
        assert response["paths_transferred"] == 3
        assert response["nar_bytes"] == 7
        assert response["compressed_bytes"] == 6
