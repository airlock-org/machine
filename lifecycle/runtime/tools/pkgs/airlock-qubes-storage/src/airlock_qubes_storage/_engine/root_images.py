"""Dom0-owned immutable root artifacts; Qubes owns each VM's writable disk."""

import os
from pathlib import Path
import shutil
import tempfile

from ..root_format import decode, encode, validate_descriptor
from .core import atomic_json, fail, read_json, safe_domain_name
from .state import generation_is_committed


def artifact_directory(root, descriptor):
    validate_descriptor(descriptor)
    return Path(root) / "root-artifacts" / descriptor["sha256"]


def reusable_root(root, descriptor):
    directory = artifact_directory(root, descriptor)
    if directory.is_symlink() or directory.parent.is_symlink():
        fail("root artifact directory must not be a symlink")
    if not directory.exists():
        return None
    image = directory / "root.img"
    if (directory.is_symlink() or image.is_symlink()
            or (directory / "artifact.json").is_symlink()
            or not image.is_file() or image.stat().st_mode & 0o222
            or read_json(directory / "artifact.json") != descriptor
            or encode(image) != descriptor):
        fail("retained root artifact failed integrity verification")
    return image


def install_root(root, payload, descriptor):
    """Caller holds the pool admission lock until its generation commits."""
    existing = reusable_root(root, descriptor)
    if existing is not None:
        return existing
    directory = artifact_directory(root, descriptor)
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="root-incoming-", dir=Path(root) / "tmp"))
    try:
        decode(payload, temporary / "root.img", descriptor)
        os.chmod(temporary / "root.img", 0o444)
        with (temporary / "root.img").open("rb") as image:
            os.fsync(image.fileno())
        atomic_json(temporary / "artifact.json", descriptor)
        fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        temporary.rename(directory)
        fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return directory / "root.img"


def source_artifact(source, physical_store_root="/nix/store"):
    path = Path(source)
    if str(path).startswith("/nix/store/"):
        path = Path(physical_store_root) / path.relative_to("/nix/store")
    if path.is_dir():
        path /= "root.img"
    # The sparse representation is canonical and contains the complete image.
    # Check the actual source, not just a descriptor supplied alongside it.
    descriptor = encode(path)
    return path, descriptor


def admit_root_source(root, source, physical_store_root="/nix/store"):
    path, descriptor = source_artifact(source, physical_store_root)
    if reusable_root(root, descriptor) is None:
        with tempfile.TemporaryDirectory(prefix="root-incoming-", dir=Path(root) / "tmp") as name:
            payload = Path(name) / "root.sparse"
            with payload.open("xb") as stream:
                if encode(path, stream) != descriptor:
                    fail("root source changed during admission")
            install_root(root, payload, descriptor)
    return descriptor


def verify_generation_root(root, manifest):
    value = manifest.get("root", {})
    descriptor = value.get("artifact")
    if not value.get("source") and descriptor is None:  # Builder views have no boot disk.
        return None
    if descriptor is None:
        fail("generation lacks a retained root artifact")
    image = reusable_root(root, descriptor)
    if image is None:
        fail("generation root artifact is missing")
    return image


def resolve_root(args):
    """Resolve only an artifact bound to this domain's committed generation."""
    domain = safe_domain_name(args.domain)
    paths = sorted((Path(args.pool_root) / "domains" / domain / "generations").glob("*/manifest.json"))
    identities = {}
    for path in paths:
        if args.generation and path.parent.name != str(args.generation):
            continue
        manifest = read_json(path)
        if (manifest.get("domain") != domain or not generation_is_committed(args.pool_root, manifest)
                or manifest.get("root", {}).get("source") != args.root_source):
            continue
        image = verify_generation_root(args.pool_root, manifest)
        identities[str(image)] = manifest["root"]["artifact"]
    if len(identities) != 1:
        fail("root source has no unique verified artifact for this domain")
    image, descriptor = next(iter(identities.items()))
    return {"image": image, "artifact": descriptor, "domain": domain}


def gc_roots(root):
    """Called under admission lock after generation GC. All remaining manifests pin roots."""
    root = Path(root)
    live = set()
    for path in (root / "domains").glob("*/generations/*/manifest.json"):
        manifest = read_json(path)  # Fail closed on damaged metadata.
        descriptor = manifest.get("root", {}).get("artifact")
        if descriptor is not None:
            live.add(validate_descriptor(descriptor)["sha256"])
    removed = []
    for directory in (root / "root-artifacts").glob("*"):
        if directory.name not in live:
            if directory.is_symlink() or not directory.is_dir():
                fail("invalid root artifact directory")
            shutil.rmtree(directory)
            removed.append(directory.name)
    return removed
