import fcntl
from contextlib import contextmanager
from pathlib import Path


from .core import (
    DEFAULT_SEGMENT_TARGET_BYTES,
    SCHEMA_POOL,
    atomic_json,
    fail,
    read_json,
    utc_now,
)


@contextmanager
def admission_lock(root):
    root = ensure_pool(root)
    with (root / "admission.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def ensure_pool(root):
    root = Path(root)
    pool_manifest = root / "MANIFEST.json"
    if (root / "objects/data.blob").exists():
        fail("unsupported single-blob closure pool; prepare a current segmented pool before activation")
    if pool_manifest.exists():
        manifest = read_json(pool_manifest)
        object_store = manifest.get("object_store") if isinstance(manifest, dict) else None
        expected = {
            "schema": "airlock.closure-store.object-store.v1",
            "segments": "objects/segments",
            "metadata": "objects/blob-metadata",
            "object_key": "nix-store-basename",
            "format": "posix-tar-fragment-v1",
        }
        if (not isinstance(manifest, dict)
                or manifest.get("schema") != SCHEMA_POOL
                or manifest.get("object_key") != "nix-store-basename"
                or not isinstance(object_store, dict)
                or "data" in object_store
                or any(object_store.get(key) != value for key, value in expected.items())):
            fail("unsupported closure-pool manifest; current segmented storage is required")
    for subdir in [
        "objects/metadata",
        "objects/blob-metadata",
        "objects/segments",
        "domains",
        "views/by-closure",
        "runtime",
        "running",
        "transactions",
        "tmp",
    ]:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    if not pool_manifest.exists():
        atomic_json(
            pool_manifest,
            {
                "schema": SCHEMA_POOL,
                "created_at": utc_now(),
                "object_key": "nix-store-basename",
                "object_store": {
                    "schema": "airlock.closure-store.object-store.v1",
                    "segments": "objects/segments",
                    "metadata": "objects/blob-metadata",
                    "object_key": "nix-store-basename",
                    "format": "posix-tar-fragment-v1",
                    "segment_target_bytes": DEFAULT_SEGMENT_TARGET_BYTES,
                },
            },
        )
    return root
