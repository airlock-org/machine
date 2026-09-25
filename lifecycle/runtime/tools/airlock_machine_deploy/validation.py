"""Pure validation of release and closure-store metadata."""

import re

from .common import SCHEMA, ADMISSION_SCHEMA, DEFAULT_TRASH_RETENTION_DAYS, DeploymentError


def validate_release_header(release) -> None:
    if release.get("schema") != SCHEMA:
        raise DeploymentError("unsupported machine release schema")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", release["profile"]):
        raise DeploymentError("invalid machine profile")
    if not release["host"].startswith("/nix/store/"):
        raise DeploymentError("host must be a built Nix system")


def validate_release_topology(release, topology) -> None:
    if topology.get("schema") != "airlock.qubes-topology.v1":
        raise DeploymentError("unsupported topology schema")
    staged = release.get("stagedGenerations", {})
    if not isinstance(staged, dict) or any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        or type(generation) is not int
        or generation < 1
        for name, generation in staged.items()
    ):
        raise DeploymentError("invalid staged generation map")
    retire = release.get("retireDomains", [])
    if (
        not isinstance(retire, list)
        or len(set(retire)) != len(retire)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            for name in retire
        )
    ):
        raise DeploymentError("invalid retired domain list")
    if set(retire) & set(topology["domains"]):
        raise DeploymentError("retired domains must be absent from the topology")
    retention = release.get("trashRetentionDays", DEFAULT_TRASH_RETENTION_DAYS)
    if not isinstance(retention, int) or not 1 <= retention <= 3650:
        raise DeploymentError("trash retention must be between 1 and 3650 days")


def validate_staged_generation(
    info, domain, generation, backend, release_manifest
):
    store = backend["store"]
    expected = {
        "domain": domain,
        "generation": generation,
        "system": backend["system"]["source"],
        "root_source": backend["root"]["source"],
        "kernel_name": backend["kernel"]["name"],
        "closure_source": store["source"],
    }
    if any(info.get(key) != value for key, value in expected.items()):
        raise DeploymentError(f"{domain}: staged generation identity mismatch")
    expected_store = {
        "mode": "shared-closure",
        "mountpoint": store["mountpoint"],
        "device": store["device"],
        "blob_device": store["blobDevice"],
        "view_backend": store["viewBackend"],
    }
    if any(
        info.get("store", {}).get(key) != value
        for key, value in expected_store.items()
    ):
        raise DeploymentError(f"{domain}: staged generation storage mismatch")
    approval = info.get("approval", {})
    provenance = info.get("provenance", {})
    if (
        approval.get("approved_by") not in {"sys-dev", "sys-ctrl"}
        or approval.get("transport") != "qrexec"
        or approval.get("source") != store["source"]
    ):
        raise DeploymentError(f"{domain}: staged generation approval mismatch")
    expected_provenance = release_manifest.get("guestProvenance", {}).get(domain, release_manifest)
    if (provenance.get("source_revision") != expected_provenance["revision"]
            or provenance.get("lock_hash") != expected_provenance.get("lockHash")):
        raise DeploymentError(f"{domain}: staged generation provenance mismatch")
    if info.get("verification", {}).get("status") != "passed":
        raise DeploymentError(f"{domain}: staged generation is not verified")
    if info.get("retention", {}).get("retain") is not True:
        raise DeploymentError(f"{domain}: staged generation is not retained")


def validate_admission_receipt(receipt) -> tuple[str, int, str, str]:
    required = {
        "schema": ADMISSION_SCHEMA,
        "status": "passed",
        "transport": "qrexec",
        "activated": False,
        "retained": True,
    }
    if not isinstance(receipt, dict) or any(
        receipt.get(key) != value for key, value in required.items()
    ):
        raise DeploymentError("receipt is not a retained inactive qrexec admission")
    domain = receipt.get("domain")
    generation = receipt.get("generation")
    revision = receipt.get("source_revision")
    lock_hash = receipt.get("lock_hash")
    if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", domain):
        raise DeploymentError("invalid receipt domain")
    if type(generation) is not int or generation < 1:
        raise DeploymentError("invalid receipt generation")
    if not isinstance(revision, str) or not re.fullmatch(
        r"[0-9a-f]{40,64}", revision
    ):
        raise DeploymentError("invalid receipt source revision")
    if not isinstance(lock_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", lock_hash
    ):
        raise DeploymentError("invalid receipt lock hash")
    return domain, generation, revision, lock_hash


def validate_volume_ownership(name, expected, inventory) -> None:
    if not expected:
        raise DeploymentError(
            "trash lacks a volume ownership snapshot; restore and retire again"
        )
    if inventory.get(name) != expected:
        raise DeploymentError(f"{name}: volume identity differs from trash")
    owned = {
        (v["pool"], v["vid"])
        for v in expected.values()
        if v["rw"] or v["save_on_stop"]
    }
    for other, volumes in inventory.items():
        if other == name:
            continue
        for volume in volumes.values():
            if (volume["pool"], volume["vid"]) in owned or (
                volume["pool"],
                volume["source"],
            ) in owned:
                raise DeploymentError(
                    f"{name}: private storage is referenced by {other}"
                )
