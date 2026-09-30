"""Root-owned, journalled whole-machine Nix deployment coordinator.

Transport and configuration evaluation deliberately live outside this module.
Qubes remains responsible for VM lifecycle, devices, policy and storage."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .common import DeploymentError, TRASH_SCHEMA
from .coordinator import Coordinator, boot_id
from .admission import AdmissionManager
from .state import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply", "prepared-check"):
        action_parser = sub.add_parser(action)
        action_parser.add_argument("release", type=Path)
        if action == "apply":
            action_parser.add_argument("--expected-state")
    bind = sub.add_parser("machine-bind")
    bind.add_argument("release", type=Path)
    bind.add_argument("receipts")
    admission = sub.add_parser("admission-prepare")
    admission.add_argument("release", type=Path)
    admission.add_argument("--caller", required=True)
    receive = sub.add_parser("admission-receive")
    receive.add_argument("argument")
    receive.add_argument("--caller", required=True)
    for action in ("guest-plan", "guest-apply"):
        sub.add_parser(action).add_argument("receipt")
    for action in (
        "status",
        "verify",
        "resume",
        "rollback",
        "trash-list",
        "release-list",
    ):
        sub.add_parser(action)
    purge = sub.add_parser("trash-purge")
    purge.add_argument("identity")
    purge.add_argument("--confirm", required=True)
    purge.add_argument("--before-expiry", action="store_true")
    for action in ("release-prune-plan", "release-prune", "release-retain"):
        command_parser = sub.add_parser(action)
        command_parser.add_argument("release", type=Path)
        if action in ("release-prune-plan", "release-prune"):
            command_parser.add_argument(
                "--allow-unexpired-trash", action="append", default=[]
            )
        if action == "release-prune":
            command_parser.add_argument("--confirm", required=True)
        if action == "release-retain":
            command_parser.add_argument(
                "--retain", required=True, choices=("true", "false")
            )
    boot = sub.add_parser("boot-setup")
    scope = boot.add_mutually_exclusive_group()
    scope.add_argument("--gui", action="store_true")
    scope.add_argument("--background", action="store_true")
    boot.add_argument("setup")
    boot.add_argument("args", nargs=argparse.REMAINDER)
    start = sub.add_parser("boot-start")
    start.add_argument("--topology", default="/etc/airlock/qubes-topology.json")
    start.add_argument("--require-running", action="append", default=[])
    args = parser.parse_args(argv)
    coordinator = Coordinator()
    try:
        if args.action == "admission-prepare":
            result = AdmissionManager(coordinator).prepare(args.release, args.caller, boot_id())
        elif args.action == "admission-receive":
            from airlock_qubes_storage._engine.artifacts import receive_incremental
            from airlock_qubes_storage._engine.core import EngineError
            token, separator, domain = args.argument.partition(".")
            if not separator:
                raise DeploymentError("candidate admission requires token and domain")
            # Match apply's lock order: machine, then pool. The candidate cannot
            # become stale halfway through publication of an inactive generation.
            with coordinator.locked():
                identity = AdmissionManager(coordinator).identity(token, domain, args.caller, boot_id())
                try:
                    result = receive_incremental(sys.stdin.buffer, sys.stdout.buffer,
                        pool_root="/var/lib/airlock/closure-store", expected_domain=domain,
                        approved_by=args.caller, expected_identity=identity)
                except EngineError as exc:
                    raise DeploymentError(str(exc)) from exc
        elif args.action == "machine-bind":
            receipts = json.load(sys.stdin) if args.receipts == "-" else read_json(args.receipts)
            result = {"candidate": str(coordinator.machine_candidate(args.release, receipts)),
                      "timings": coordinator.binding_timings}
        elif args.action == "plan":
            result = coordinator.inspect(args.release)
        elif args.action == "prepared-check":
            result = coordinator.check_prepared(args.release)
        elif args.action == "apply":
            result = coordinator.apply(args.release, expected_state=args.expected_state)
        elif args.action in ("guest-plan", "guest-apply"):
            receipt = (
                json.load(sys.stdin) if args.receipt == "-" else read_json(args.receipt)
            )
            candidate = coordinator.guest_candidate(receipt)
            result = (
                coordinator.apply(candidate)
                if args.action == "guest-apply"
                else {
                    "candidate": str(candidate),
                    "plan": coordinator.inspect(candidate),
                }
            )
        elif args.action in ("resume", "rollback"):
            status = coordinator.status()
            key = "release" if args.action == "resume" else "previous"
            if key not in status:
                raise DeploymentError("no deployment to resume or roll back")
            result = coordinator.apply(status[key], recover=True)
        elif args.action == "boot-setup":
            if args.gui or args.background:
                coordinator.boot_setup(args.setup, args.args, gui=args.gui, background=args.background)
            else:
                coordinator.boot_setup(args.setup, args.args)
            return 0
        elif args.action == "boot-start":
            result = coordinator.boot_start(args.topology, args.require_running)
        elif args.action == "verify":
            result = coordinator.verify_selected()
        elif args.action == "trash-list":
            result = {"schema": TRASH_SCHEMA, "records": coordinator.retirement.trash_records()}
        elif args.action == "trash-purge":
            result = coordinator.retirement.purge_trash(
                args.identity, args.confirm, before_expiry=args.before_expiry
            )
        elif args.action == "release-prune-plan":
            result = coordinator.releases.prune_plan(
                args.release, early_purge_trash=args.allow_unexpired_trash
            )
        elif args.action == "release-list":
            result = coordinator.releases.list_releases()
        elif args.action == "release-prune":
            result = coordinator.releases.prune_release(
                args.release, args.confirm,
                early_purge_trash=args.allow_unexpired_trash,
            )
        elif args.action == "release-retain":
            result = coordinator.releases.retain_release(args.release, args.retain == "true")
        else:
            result = coordinator.status()
        print(json.dumps(result, sort_keys=True, indent=None if args.action == "admission-receive" else 2))
        return 0
    except (DeploymentError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        if args.action == "admission-receive":
            from airlock_qubes_storage._engine.artifacts import SCHEMA_ERROR, write_message
            try:
                write_message(sys.stdout.buffer, {"schema": SCHEMA_ERROR, "error": str(exc)[:4096]})
            except OSError:
                pass
        print(f"airlock-machine-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
