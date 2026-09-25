import argparse
import json
import os
import subprocess
import sys
import time

from ._engine.artifacts import (
    SCHEMA_ERROR,
    read_message,
    receive_artifact,
    receive_incremental,
    write_artifact,
    write_message,
)
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
    incremental = sub.add_parser("receive-incremental")
    incremental.add_argument("--pool-root", default="/var/lib/airlock/closure-store")
    incremental.add_argument("--expected-domain", required=True)
    incremental.add_argument("--approved-by", required=True)
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
        receiver = (
            receive_incremental
            if args.command == "receive-incremental"
            else receive_artifact
        )
        extra = (
            {"output": sys.stdout.buffer}
            if args.command == "receive-incremental"
            else {}
        )
        result = receiver(
            sys.stdin.buffer,
            pool_root=args.pool_root,
            expected_domain=args.expected_domain,
            approved_by=args.approved_by,
            **extra,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (EngineError, OSError, ValueError) as exc:
        print(f"airlock-pool-artifact: {exc}", file=sys.stderr)
        if args.command == "receive-incremental":
            # qrexec records dom0 stderr locally; report rejection over the
            # duplex protocol too, at either negotiation or final receipt.
            try:
                write_message(
                    sys.stdout.buffer,
                    {"schema": SCHEMA_ERROR, "error": str(exc)[:4096]},
                )
            except OSError:
                pass  # A disconnected sender must not hide the original error.
        return 1


def read_receiver_message(stream):
    result = read_message(stream)
    if isinstance(result, dict) and result.get("schema") == SCHEMA_ERROR:
        error = result.get("error")
        if not isinstance(error, str) or not error or len(error) > 4096:
            raise EngineError("invalid pool receiver error response")
        raise EngineError(f"pool receiver rejected admission: {error}")
    return result


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
                f"airlock.Pool.AdmitIncremental+{args.domain}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        started = time.monotonic()
        timings = {}

        def negotiate(inventory):
            timings["sender_inventory_seconds"] = time.monotonic() - started
            write_message(process.stdin, inventory)
            response = read_receiver_message(process.stdout)
            timings["sender_negotiation_seconds"] = (
                time.monotonic() - started - timings["sender_inventory_seconds"])
            return response

        inventory = write_artifact(process.stdin, negotiate=negotiate, **prepare_kwargs(args))
        timings["sender_payload_seconds"] = time.monotonic() - started - sum(timings.values())
        process.stdin.close()
        result = read_receiver_message(process.stdout)
        code = process.wait()
        if code:
            return code
        if (
            not isinstance(result, dict)
            or result.get("status") != "passed"
            or result.get("domain") != args.domain
            or result.get("system") != args.system
            or result.get("activated") is not False
            or result.get("retained") is not True
            or (inventory.get("root_artifact") is not None
                and result.get("root_artifact") != inventory["root_artifact"])
        ):
            raise EngineError("invalid pool admission response")
        result.setdefault("timings", {}).update(timings)
        result["payload_encoding"] = "tar-with-canonical-sparse-root"
        print(json.dumps(result, sort_keys=True))
        return 0
    except (EngineError, OSError, ValueError) as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        print(f"airlock-pool-send: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
