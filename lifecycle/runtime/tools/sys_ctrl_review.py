"""Detached controller jobs and the trusted pre-build review window."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

REQUIRED = Path("/etc/airlock/controller-review-required")
ACTIVE = {"queued", "awaiting_approval", "building"}
EXITS = {"rejected": 20, "cancelled": 21, "scope_exceeded": 22, "stale_review": 23, "failed": 1}

class Stopped(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status

def user_env():
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/" + str(os.getuid()))
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + env["XDG_RUNTIME_DIR"] + "/bus"
    return env

def control(request):
    result = subprocess.run(["qrexec-client-vm", "dom0", "airlock.Ctrl.ReviewControl"],
        input=json.dumps(request) + "\n", text=True, capture_output=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "review control failed")[-4096:])
    return json.loads(result.stdout)

def job_path(state, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("invalid request ID")
    return state / "jobs" / identity / "status.json"

def status(state, identity):
    value = json.loads(job_path(state, identity).read_text())
    if value["status"] in {"building", "deployment_started"} and value.get("authorization_id"):
        latest = control({"op": "status", "request_id": value["authorization_id"]})
        if latest["status"] in {"scope_exceeded", "deployment_started"}:
            value.update(status=latest["status"], plan=latest.get("plan"), release=latest.get("release"))
    if value["status"] in ACTIVE and time.time() - value.get("updated_at", value.get("created_at", time.time())) > 10:
        running = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "airlock-review-" + identity], env=user_env())
        if running.returncode:
            value.update(status="cancelled" if value["status"] == "awaiting_approval" else "failed",
                         error="Controller job was interrupted; no new approval was granted by reconnecting.")
    return value

def reconcile(deploy, identity):
    value = status(deploy.STATE, identity)
    deploy.record(job_path(deploy.STATE, identity), value)
    return value

def enqueue(deploy, header):
    identity = uuid.uuid4().hex
    path = job_path(deploy.STATE, identity)
    deploy.record(path, {"request_id": identity, "status": "queued", "request": header,
                         "created_at": time.time()})
    try:
        subprocess.run(["systemd-run", "--user", "--collect", "--unit=airlock-review-" + identity,
                        "--property=Type=exec", sys.executable, str(Path(deploy.__file__).resolve()),
                        "work", identity], env=user_env(), check=True, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        value = status(deploy.STATE, identity)
        value.update(status="failed", error="Could not start controller job: " + str(exc))
        deploy.record(path, value)
    return identity

def wait(deploy, identity):
    previous = None
    while True:
        value = status(deploy.STATE, identity)
        if value.get("decision") == "approved":
            # The worker can advance through several phases between polls. Keep
            # its durable decision separate from its current work status.
            value = {**value, "status": "approved", "job_status": value["status"],
                     "continuing": value["status"] in ACTIVE,
                     "status_command": "airlock machine review-status --request-id " + identity}
        if value["status"] != previous:
            print(json.dumps(value, sort_keys=True), flush=True)
            previous = value["status"]
        if previous not in ACTIVE:
            return EXITS.get(previous, 0)
        time.sleep(0.4)

def authorize(deploy, header, update):
    family = "machine" if header["action"].startswith("machine-") else "guest"
    context = control({"op": "context", "revision": header["revision"],
                       "lock_hash": header["lock_hash"], "family": family, "target": header["target"]})
    identity = context["request_id"]
    update("awaiting_approval", authorization_id=identity)
    if context["status"] != "awaiting_approval":
        if context["status"] != "approved":
            raise Stopped(context["status"])
    else:
        request = {"schema": "airlock.ctrl-approval-request.v1", "request_id": identity,
                   "baseline": context["baseline"], **context["binding"], "scope": context["scope"]}
        path = deploy.STATE / "reviews" / (identity + ".json")
        deploy.record(path, request)
        env = user_env()
        exported = subprocess.check_output(["systemctl", "--user", "show-environment"], env=env, text=True)
        for line in exported.splitlines():
            name, _, value = line.partition("=")
            if name in {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XCURSOR_SIZE", "GDK_SCALE", "GDK_DPI_SCALE"}:
                env[name] = value
        if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
            raise ValueError("controller graphical session is unavailable; no build was started")
        app = os.environ.get("AIRLOCK_CTRL_BIN", "/run/current-system/sw/bin/airlock-ctrl")
        result = subprocess.run([app, "--repo", str(deploy.REPO), "--base", context["baseline"],
                                 "--candidate", header["revision"], "--approval-request", str(path)],
                                env=env, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("review window failed; no build was started: " + result.stderr[-2048:])
        reply = json.loads(result.stdout)
        if (set(reply) != {"schema", "request_id", "revision", "lock_hash", "review_fingerprint", "decision", "scope"}
                or reply["schema"] != "airlock.ctrl-approval-response.v1"
                or any(reply[key] != request[key] for key in ("request_id", "revision", "lock_hash"))):
            raise ValueError("review reply differs from submitted candidate")
        context = control({"op": "decide", "request_id": identity, "decision": reply["decision"],
                           "scope": reply["scope"], "review_fingerprint": reply["review_fingerprint"]})
        if context["status"] != "approved":
            raise Stopped(context["status"])
    control({"op": "check", "request_id": identity, "revision": header["revision"], "lock_hash": header["lock_hash"]})
    return identity

def work(deploy, identity):
    path = job_path(deploy.STATE, identity)
    value = status(deploy.STATE, identity)
    header = value["request"]
    def update(phase, **extra):
        value.update(status=phase, updated_at=time.time(), **extra)
        deploy.record(path, value)
    try:
        if deploy.identity(deploy.REPO) != (header["revision"], header["lock_hash"]):
            raise ValueError("controller source changed before review")
        deploy.APPROVAL = authorize(deploy, header, update)
        if deploy.identity(deploy.REPO) != (header["revision"], header["lock_hash"]):
            raise ValueError("controller source changed during review")
        update("building", decision="approved")
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            deploy.execute(header["action"], header["target"], header["revision"], header["lock_hash"],
                           assistant_rename=header["schema"] == deploy.RENAME_SCHEMA)
        payload = json.loads(result.getvalue())
        phase = "deployment_started" if "unit" in payload else "planned" if header["action"].endswith("plan") else "built"
        update(phase, result=payload)
        return 0
    except Stopped as exc:
        update(exc.status)
        return EXITS.get(exc.status, 1)
    except Exception as exc:
        detail = str(exc)[-4096:]
        phase = next((name for name in ("scope_exceeded", "stale_review") if name in detail), "failed")
        update(phase, error=detail)
        return EXITS[phase]
