import argparse
import json
import os
import subprocess
import sys

from ._engine.artifacts import receive_artifact, write_artifact
from ._engine.core import EngineError, default_physical_store_root


def add_prepare_arguments(parser):
    parser.add_argument("--domain", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--closure-source", required=True)
    parser.add_argument("--root-source", default="")
    parser.add_argument("--kernel-name", default="")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--lock-hash", required=True)
    parser.add_argument("--physical-store-root", default=default_physical_store_root())
    parser.add_argument("--nix", default="nix")
    parser.add_argument(
        "--nix-store", default=os.environ.get("AIRLOCK_POOL_NIX_STORE", "")
    )
    parser.add_argument("--path-info-json")


def build_parser():
    parser = argparse.ArgumentParser(prog="airlock-pool-artifact")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    add_prepare_arguments(prepare)

    receive = sub.add_parser("receive")
    receive.add_argument("--pool-root", default="/var/lib/airlock/closure-store")
    receive.add_argument("--expected-domain", required=True)
    receive.add_argument("--approved-by", required=True)
    return parser


def prepare_kwargs(args):
    path_info = None
    if args.path_info_json:
        with open(args.path_info_json, encoding="utf-8") as stream:
            path_info = json.load(stream)
    return {
        "domain": args.domain,
        "system": args.system,
        "closure_source": args.closure_source,
        "root_source": args.root_source,
        "kernel_name": args.kernel_name,
        "source_revision": args.source_revision,
        "lock_hash": args.lock_hash,
        "physical_store_root": args.physical_store_root,
        "nix": args.nix,
        "nix_store": args.nix_store or None,
        "path_info": path_info,
    }


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            write_artifact(sys.stdout.buffer, **prepare_kwargs(args))
            return 0
        result = receive_artifact(
            sys.stdin.buffer,
            pool_root=args.pool_root,
            expected_domain=args.expected_domain,
            approved_by=args.approved_by,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (EngineError, OSError, ValueError) as exc:
        print(f"airlock-pool-artifact: {exc}", file=sys.stderr)
        return 1


def send_main(argv=None):
    parser = argparse.ArgumentParser(prog="airlock-pool-send")
    add_prepare_arguments(parser)
    parser.add_argument("--qrexec-client", default="qrexec-client-vm")
    args = parser.parse_args(argv)
    process = None
    try:
        process = subprocess.Popen(
            [
                args.qrexec_client,
                "dom0",
                f"airlock.Pool.Admit+{args.domain}",
            ],
            stdin=subprocess.PIPE,
        )
        write_artifact(process.stdin, **prepare_kwargs(args))
        process.stdin.close()
        return process.wait()
    except (EngineError, OSError, ValueError) as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        print(f"airlock-pool-send: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
