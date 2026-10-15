"""Human authorization must precede builds and bound the eventual activation."""
import importlib
import json
import multiprocessing
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lifecycle/runtime/tools"))
review = importlib.import_module("airlock_machine_deploy.review")
jobs = importlib.import_module("sys_ctrl_review")
state = importlib.import_module("airlock_machine_deploy.state")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    marker = tmp_path / "required"
    marker.touch()
    monkeypatch.setattr(review, "MARKER", marker)
    baseline = {"revision": "a" * 40, "lockHash": "b" * 64}
    candidate = {"revision": "c" * 40, "lockHash": "d" * 64}
    plan = {"changed": ["usr-home"], "stop": ["usr-home"], "start": ["usr-home"], "host_reboot_required": False}
    coordinator = SimpleNamespace(state=tmp_path, pointer=lambda: {"release": "baseline"},
        baseline=lambda: "baseline", inspect=lambda _: plan)
    monkeypatch.setattr(review, "load_release", lambda path: (
        baseline if str(path) == "baseline" else candidate, {"domains": {"sys-ctrl": {}, "usr-home": {}}}))
    instance = review.Reviews(coordinator)
    instance.baseline_manifest = baseline
    instance.candidate = candidate
    instance.plan = plan
    return instance


def pending(manager):
    return manager.context({"op": "context", "revision": "c" * 40, "lock_hash": "d" * 64,
                            "family": "machine", "target": None})


def approve(manager):
    request = pending(manager)
    return manager.decide({"op": "decide", "request_id": request["request_id"], "decision": "approve",
                           "scope": request["scope"], "review_fingerprint": "e" * 64})


def test_pending_cannot_build_and_same_candidate_reuses_one_approval(manager):
    request = pending(manager)
    with pytest.raises(review.DeploymentError, match="not approved"):
        manager.require(request["request_id"], "c" * 40, "d" * 64)
    approved = approve(manager)
    assert pending(manager)["request_id"] == approved["request_id"]
    assert pending(manager)["status"] == "approved"
    with pytest.raises(review.DeploymentError, match="source differs"):
        manager.require(approved["request_id"], "f" * 40, "d" * 64)
    manager.coordinator.pointer = lambda: {"release": "changed"}
    with pytest.raises(review.DeploymentError, match="stale_review"):
        manager.require(approved["request_id"], "c" * 40, "d" * 64)
    assert pending(manager)["request_id"] != approved["request_id"]


def test_baseline_lock_change_invalidates_approval_without_revision_change(manager):
    approved = approve(manager)
    manager.baseline_manifest["lockHash"] = "f" * 64
    with pytest.raises(review.DeploymentError, match="stale_review"):
        manager.require(approved["request_id"], "c" * 40, "d" * 64)
    assert pending(manager)["request_id"] != approved["request_id"]


@pytest.mark.parametrize("lock_hash", [None, "", "g" * 64, "a" * 63, 123])
def test_review_rejects_baseline_without_valid_lock_identity(manager, lock_hash):
    manager.baseline_manifest["lockHash"] = lock_hash
    with pytest.raises(review.DeploymentError, match="baseline is unavailable"):
        pending(manager)


@pytest.mark.parametrize("decision,status", [("reject", "rejected"), ("cancelled", "cancelled")])
def test_negative_decision_is_durable_and_cannot_be_replayed(manager, decision, status):
    request = pending(manager)
    result = manager.decide({"op": "decide", "request_id": request["request_id"], "decision": decision,
                            "scope": request["scope"], "review_fingerprint": "e" * 64})
    assert result["status"] == status
    assert pending(manager)["status"] == status
    with pytest.raises(review.DeploymentError, match="not approved"):
        manager.require(request["request_id"], "c" * 40, "d" * 64)


@pytest.mark.parametrize("change", [
    {"stop": ["unapproved"]}, {"start": ["new-vm"]}, {"retire": ["unknown"]},
    {"private_copies": [{"source": {"domain": "unknown"}, "target_name": "usr-home"}]},
])
def test_final_plan_must_stay_within_scope(manager, change):
    approved = approve(manager)
    manager.plan.update(change)
    with pytest.raises(review.DeploymentError, match="scope_exceeded"):
        manager.bind(approved["request_id"], "candidate")
    assert not (manager.root / "bindings").exists()
    assert pending(manager)["status"] == "scope_exceeded"


@pytest.mark.parametrize("reboot_required", [False, True])
def test_single_approval_covers_reboot_and_reports_final_requirement(manager, reboot_required):
    approved = approve(manager)
    manager.plan["host_reboot_required"] = reboot_required
    bound = manager.bind(approved["request_id"], "candidate")
    assert bound["status"] == "approved"
    assert bound["plan"]["host_reboot_required"] is reboot_required
    manager.activation("candidate", manager.candidate, manager.plan)


@pytest.mark.parametrize("reboot_required", [True, None])
def test_existing_reboot_restriction_is_not_silently_broadened(manager, reboot_required):
    request = pending(manager)
    manager.decide({"op": "decide", "request_id": request["request_id"], "decision": "approve",
                    "scope": {**request["scope"], "allow_reboot": False}, "review_fingerprint": "e" * 64})
    manager.plan["host_reboot_required"] = reboot_required
    with pytest.raises(review.DeploymentError, match="system reboot was not permitted"):
        manager.bind(request["request_id"], "candidate")
    assert not (manager.root / "bindings").exists()


def test_guard_rechecks_plan_at_activation_and_preserves_existing_recovery(manager):
    approved = approve(manager)
    manager.bind(approved["request_id"], "candidate")
    manager.plan["stop"] = ["unapproved"]
    with pytest.raises(review.DeploymentError, match="scope_exceeded"):
        manager.activation("candidate", manager.candidate, manager.plan)
    assert pending(manager)["status"] == "scope_exceeded"
    # Typed recovery can resume/roll back already selected generations without
    # granting approval to a new submission, including when sys-ctrl is down.
    manager.activation("previous-recorded-generation", manager.candidate, manager.plan, recover=True)


def test_activation_without_bound_human_authorization_fails(manager):
    with pytest.raises(review.DeploymentError, match="requires a human-approved"):
        manager.activation("candidate", manager.candidate, manager.plan)


@pytest.mark.parametrize("value", [
    {"allowed_vms": ["../usr-home"], "allow_reboot": False},
    {"allowed_vms": ["usr-home"], "allow_reboot": "false"},
    {"allowed_vms": [], "allow_reboot": False},
    {"allowed_vms": ["usr-home"], "allow_reboot": False, "override": True},
])
def test_invalid_scope_cannot_grant_authority(value):
    with pytest.raises(review.DeploymentError):
        review.scope(value)


def test_guest_authorization_cannot_activate_whole_machine(manager):
    request = manager.context({"op": "context", "revision": "c" * 40, "lock_hash": "d" * 64,
                               "family": "guest", "target": "usr-home"})
    manager.decide({"op": "decide", "request_id": request["request_id"], "decision": "approve",
                    "scope": request["scope"], "review_fingerprint": "e" * 64})
    with pytest.raises(review.DeploymentError, match="guest approval differs"):
        manager.bind(request["request_id"], "candidate")


@pytest.fixture
def worker(tmp_path):
    identity = "a" * 32
    header = {"schema": "schema", "action": "machine-apply", "target": None,
              "revision": "b" * 40, "lock_hash": "c" * 64}
    deploy = SimpleNamespace(STATE=tmp_path, REPO=tmp_path, RENAME_SCHEMA="rename", record=state.atomic_json,
                             identity=lambda _: (header["revision"], header["lock_hash"]))
    state.atomic_json(jobs.job_path(tmp_path, identity), {"status": "queued", "request_id": identity, "request": header})
    return deploy, identity


@pytest.mark.parametrize("phase", ["rejected", "cancelled", "scope_exceeded"])
def test_worker_returns_negative_feedback_without_evaluation_or_build(worker, monkeypatch, phase):
    deploy, identity = worker
    def deny(*_):
        raise jobs.Stopped(phase)
    monkeypatch.setattr(jobs, "authorize", deny)
    deploy.execute = lambda *_a, **_k: pytest.fail("unapproved evaluation or build")
    assert jobs.work(deploy, identity) == jobs.EXITS[phase]
    assert jobs.status(deploy.STATE, identity)["status"] == phase


def test_worker_checks_source_after_approval_before_build(worker, monkeypatch):
    deploy, identity = worker
    def authorize(*_):
        deploy.identity = lambda _: ("changed", "changed")
        return "d" * 64
    monkeypatch.setattr(jobs, "authorize", authorize)
    deploy.execute = lambda *_a, **_k: pytest.fail("changed source was built")
    assert jobs.work(deploy, identity) == 1
    assert "source changed during review" in jobs.status(deploy.STATE, identity)["error"]


def test_approved_job_reports_submission_not_deployment_completion(worker, monkeypatch):
    deploy, identity = worker
    monkeypatch.setattr(jobs, "authorize", lambda *_: "d" * 64)
    deploy.execute = lambda *_a, **_k: print(json.dumps({"unit": "detached-dom0-worker"}))
    assert jobs.work(deploy, identity) == 0
    assert jobs.status(deploy.STATE, identity)["status"] == "deployment_started"


def _run_approved_worker(directory, identity, building, finish):
    header = json.loads(jobs.job_path(directory, identity).read_text())["request"]
    def execute(*_a, **_k):
        building.set()
        if not finish.wait(5):
            raise TimeoutError("test did not release the build")
        print(json.dumps({"unit": "detached-dom0-worker"}))

    deploy = SimpleNamespace(STATE=directory, REPO=directory, RENAME_SCHEMA="rename",
                             record=state.atomic_json, execute=execute,
                             identity=lambda _: (header["revision"], header["lock_hash"]))
    jobs.authorize = lambda *_: "d" * 64
    raise SystemExit(jobs.work(deploy, identity))


def test_submitter_returns_approval_while_worker_keeps_building(worker, monkeypatch, capsys):
    deploy, identity = worker
    context = multiprocessing.get_context("spawn")
    building, finish = context.Event(), context.Event()
    monkeypatch.setattr(jobs, "control", lambda _: {"status": "approved"})
    process = context.Process(target=_run_approved_worker,
                              args=(deploy.STATE, identity, building, finish))
    process.start()
    try:
        assert building.wait(5), "worker never reached the build"
        monkeypatch.setattr(jobs.time, "sleep", lambda _: pytest.fail("wait continued after approval"))
        assert jobs.wait(deploy, identity) == 0
        reply = json.loads(capsys.readouterr().out)
        assert reply["status"] == reply["decision"] == "approved"
        assert reply["request_id"] == identity
        assert reply["job_status"] == "building"
        assert reply["continuing"] is True
        assert reply["status_command"] == "airlock machine review-status --request-id " + identity
        assert process.is_alive(), "submission waited for the worker to finish"
        finish.set()
        process.join(5)
        assert process.exitcode == 0
        outcome = jobs.status(deploy.STATE, identity)
        assert outcome["decision"] == "approved"
        assert outcome["status"] == "deployment_started"
    finally:
        finish.set()
        if process.is_alive():
            process.terminate()
        process.join(5)
        process.close()


@pytest.mark.parametrize("phase", ["deployment_started", "planned", "built", "failed", "scope_exceeded"])
def test_fast_worker_preserves_approval_and_reports_current_outcome(worker, monkeypatch, capsys, phase):
    deploy, identity = worker
    deploy.record(jobs.job_path(deploy.STATE, identity),
                  {"request_id": identity, "status": phase, "decision": "approved"})
    assert jobs.wait(deploy, identity) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["status"] == "approved"
    assert reply["job_status"] == phase
    assert reply["continuing"] is False
    # Querying the job still returns its work outcome, not the submission reply.
    assert jobs.status(deploy.STATE, identity)["status"] == phase


@pytest.mark.parametrize("phase", ["rejected", "cancelled", "failed", "stale_review", "scope_exceeded"])
def test_wait_streams_pending_and_negative_feedback(monkeypatch, capsys, phase):
    values = iter({"status": status} for status in ("queued", "awaiting_approval", phase))
    monkeypatch.setattr(jobs, "status", lambda *_: next(values))
    monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
    assert jobs.wait(SimpleNamespace(STATE=None), "id") == jobs.EXITS[phase]
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == ["queued", "awaiting_approval", phase]


@pytest.mark.parametrize("phase", ["scope_exceeded", "deployment_started"])
def test_status_recovers_dom0_outcome_after_controller_restart(worker, monkeypatch, phase):
    deploy, identity = worker
    path = jobs.job_path(deploy.STATE, identity)
    deploy.record(path, {"request_id": identity, "status": "building", "authorization_id": "d" * 64,
                         "updated_at": 0})
    monkeypatch.setattr(jobs, "control", lambda _: {"status": phase, "release": "candidate"})
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k: pytest.fail("terminal root outcome was lost"))
    assert jobs.status(deploy.STATE, identity)["status"] == phase
    assert json.loads(path.read_text())["status"] == "building"


def test_interrupted_review_is_cancelled_without_reopening_or_building(worker, monkeypatch):
    deploy, identity = worker
    deploy.record(jobs.job_path(deploy.STATE, identity),
                  {"request_id": identity, "status": "awaiting_approval", "updated_at": 0})
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=3))
    assert jobs.status(deploy.STATE, identity)["status"] == "cancelled"
    assert json.loads(jobs.job_path(deploy.STATE, identity).read_text())["status"] == "awaiting_approval"


def test_status_query_cannot_overwrite_worker_progress(worker, monkeypatch):
    deploy, identity = worker
    path = jobs.job_path(deploy.STATE, identity)
    deploy.record(path, {"status": "building", "authorization_id": "d" * 64,
                         "updated_at": 0})

    def root_status(_):
        # The worker finishes after the query reads its original snapshot.
        deploy.record(path, {"status": "deployment_started", "decision": "approved",
                             "result": {"unit": "detached-worker"}})
        return {"status": "approved"}

    monkeypatch.setattr(jobs, "control", root_status)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    assert jobs.status(deploy.STATE, identity)["status"] == "building"
    assert json.loads(path.read_text())["result"] == {"unit": "detached-worker"}


@pytest.mark.parametrize("stdout,stderr,code", [
    ("", "", 1),
    ("", "policy refused", 126),
    (json.dumps({"schema": "airlock.review-control-error.v1", "status": "busy",
                 "error": "Automatic maintenance holds the lock", "lock": {"owner": "Maintenance"}}), "", 1),
    ("invalid response", "", 0),
    ("{}", "", 0),
])
def test_control_failures_preserve_context_and_transport_diagnostics(monkeypatch, stdout, stderr, code):
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr))
    with pytest.raises(jobs.ControlError) as error:
        jobs.control({"op": "status", "request_id": "d" * 64})
    assert error.value.details["exit_code"] == code
    assert error.value.details["operation"] == "status"
    assert error.value.details["request_id"] == "d" * 64
    assert error.value.details["stdout"] == stdout
    assert error.value.details["stderr"] == stderr
    assert f"exit {code}" in str(error.value)
    if "Automatic maintenance" in stdout:
        assert "Automatic maintenance" in str(error.value)
        assert error.value.details["response"]["lock"]["owner"] == "Maintenance"


def test_approval_feedback_survives_status_transport_failure(worker, monkeypatch, capsys):
    deploy, identity = worker
    path = jobs.job_path(deploy.STATE, identity)
    deploy.record(path, {"request_id": identity, "status": "building", "decision": "approved",
                         "authorization_id": "d" * 64})
    original = path.read_bytes()
    def unavailable(request):
        raise jobs.ControlError(request, SimpleNamespace(returncode=1, stdout="", stderr=""),
                                "No diagnostic output received")
    monkeypatch.setattr(jobs, "control", unavailable)
    assert jobs.wait(deploy, identity) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["status"] == "approved"
    assert reply["job_status"] == "building"
    assert reply["control_error"]["exit_code"] == 1
    assert "No diagnostic output" in reply["observation_error"]
    assert path.read_bytes() == original


def test_worker_finishing_during_liveness_check_is_not_reported_as_interrupted(worker, monkeypatch):
    deploy, identity = worker
    path = jobs.job_path(deploy.STATE, identity)
    deploy.record(path, {"status": "building", "updated_at": 0})
    def finished(*_a, **_k):
        deploy.record(path, {"status": "planned", "result": {"plan": "complete"}})
        return SimpleNamespace(returncode=3)
    monkeypatch.setattr(jobs.subprocess, "run", finished)
    assert jobs.status(deploy.STATE, identity)["status"] == "planned"
