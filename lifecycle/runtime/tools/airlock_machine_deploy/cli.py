"""Root-owned, journalled whole-machine Nix deployment coordinator.

Transport and configuration evaluation deliberately live outside this module.
Qubes remains responsible for VM lifecycle, devices, policy and storage."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .common import DeploymentError, TRASH_SCHEMA
from .coordinator import Coordinator
from .state import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        sub.add_parser(action).add_argument("release", type=Path)
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
    for action in ("release-prune-plan", "release-prune", "release-retain"):
        command_parser = sub.add_parser(action)
        command_parser.add_argument("release", type=Path)
        if action == "release-prune":
            command_parser.add_argument("--confirm", required=True)
        if action == "release-retain":
            command_parser.add_argument(
                "--retain", required=True, choices=("true", "false")
            )
    boot = sub.add_parser("boot-setup")
    boot.add_argument("setup")
    boot.add_argument("args", nargs=argparse.REMAINDER)
    start = sub.add_parser("boot-start")
    start.add_argument("--topology", default="/etc/airlock/qubes-topology.json")
    start.add_argument("--require-running", action="append", default=[])
    args = parser.parse_args(argv)
    coordinator = Coordinator()
    try:
        if args.action == "plan":
            result = coordinator.inspect(args.release)
        elif args.action == "apply":
            result = coordinator.apply(args.release)
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
            coordinator.boot_setup(args.setup, args.args)
            return 0
        elif args.action == "boot-start":
            result = coordinator.boot_start(args.topology, args.require_running)
        elif args.action == "verify":
            result = coordinator.verify_selected()
        elif args.action == "trash-list":
            result = {"schema": TRASH_SCHEMA, "records": coordinator.retirement.trash_records()}
        elif args.action == "trash-purge":
            result = coordinator.retirement.purge_trash(args.identity, args.confirm)
        elif args.action == "release-prune-plan":
            result = coordinator.releases.prune_plan(args.release)
        elif args.action == "release-list":
            result = coordinator.releases.list_releases()
        elif args.action == "release-prune":
            result = coordinator.releases.prune_release(args.release, args.confirm)
        elif args.action == "release-retain":
            result = coordinator.releases.retain_release(args.release, args.retain == "true")
        else:
            result = coordinator.status()
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DeploymentError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"airlock-machine-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
