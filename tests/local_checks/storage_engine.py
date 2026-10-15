from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from .support import _run, _write_executable


def _storage_env(repo_root: Path, extra_path: str = "") -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(
        repo_root
        / "lifecycle/runtime/tools/pkgs/airlock-qubes-storage/src"
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    path_parts = [extra_path] if extra_path else []
    if shutil.which("mkfs.erofs") is None or shutil.which("fsck.erofs") is None:
        import pytest

        pytest.skip("requires erofs-utils on PATH; test setup does not build tools")
    if path_parts:
        env["PATH"] = ":".join(path_parts + [env["PATH"]])
    return env


def _run_storage(
    repo_root: Path,
    result_dir: Path,
    name: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    result = _run(
        ["python3", "-m", "airlock_qubes_storage.cli", *args],
        cwd=repo_root,
        env=env or _storage_env(repo_root),
        stdout=result_dir / f"{name}.json",
        stderr=result_dir / f"{name}.err",
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"storage command failed: {' '.join(args)}\n"
            f"{(result_dir / f'{name}.err').read_text(encoding='utf-8')}"
        )
    payload = None
    if (result_dir / f"{name}.json").stat().st_size:
        payload = json.loads((result_dir / f"{name}.json").read_text())
    return result, payload


def _make_storage_object(
    physical_store: Path, entry: str, content: str, size: int = 1024
) -> None:
    target = physical_store / entry / "bin/payload"
    target.parent.mkdir(parents=True)
    data = (content + "\n").encode("utf-8")
    target.write_bytes((data * ((size // len(data)) + 1))[:size])
    (physical_store / entry).chmod(0o555)
    (physical_store / entry / "bin").chmod(0o555)
    target.chmod(0o444)


def _make_storage_closure(work_dir: Path, name: str, entries: list[str]) -> Path:
    closure = work_dir / "closures" / name
    closure.mkdir(parents=True)
    (closure / "store-paths").write_text(
        "".join(f"/nix/store/{entry}\n" for entry in entries), encoding="utf-8"
    )
    (closure / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "airlock.store-closure-manifest.v1",
                "name": name,
                "store_paths_file": "store-paths",
                "read_only": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return closure


def _object_segment(pool: Path, entry: str) -> str:
    return json.loads((pool / f"objects/blob-metadata/{entry}.json").read_text())[
        "segment_id"
    ]


def _run_storage_failpoint(
    repo_root: Path,
    result_dir: Path,
    name: str,
    failpoint: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    fail_env = (env or _storage_env(repo_root)).copy()
    fail_env["AIRLOCK_CLOSURE_STORE_FAILPOINT"] = failpoint
    result, _payload = _run_storage(
        repo_root, result_dir, name, args, env=fail_env, check=False
    )
    assert result.returncode != 0
    assert f"simulated failure at {failpoint}" in (
        result_dir / f"{name}.err"
    ).read_text(encoding="utf-8")


def check_closure_store_engine(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    work_dir = result_dir / "work"
    if work_dir.exists():
        _run(["chmod", "-R", "u+w", str(work_dir)], check=False)
    shutil.rmtree(result_dir, ignore_errors=True)
    (work_dir / "physical-store").mkdir(parents=True)
    (work_dir / "closures").mkdir()
    (work_dir / "bin").mkdir()
    result_dir.mkdir(parents=True, exist_ok=True)
    physical_store = work_dir / "physical-store"
    root_image = work_dir / "root.img"
    with root_image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
        stream.seek(1080)
        stream.write(b"\x53\xef")

    entries = {
        "common": "11111111111111111111111111111111-common",
        "work1": "22222222222222222222222222222222-work-system-1",
        "unique1": "33333333333333333333333333333333-work-unique-1",
        "work2": "44444444444444444444444444444444-work-system-2",
        "unique2": "55555555555555555555555555555555-work-unique-2",
        "small_a": "dddddddddddddddddddddddddddddddd-small-a",
        "small_b": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-small-b",
        "small_c": "ffffffffffffffffffffffffffffffff-small-c",
    }
    for key, entry in entries.items():
        _make_storage_object(
            physical_store, entry, key, 12000 if "work" in key else 1024
        )
    _make_storage_closure(
        work_dir, "work-gen1", [entries["common"], entries["work1"], entries["unique1"]]
    )
    _make_storage_closure(
        work_dir, "work-gen2", [entries["common"], entries["work2"], entries["unique2"]]
    )
    _make_storage_closure(
        work_dir, "fit", [entries["small_a"], entries["small_b"], entries["small_c"]]
    )
    fake_nix = work_dir / "bin/nix"
    _write_executable(
        fake_nix,
        """
        #!/usr/bin/env bash
        printf '%s\n' "$@" > "$AIRLOCK_FAKE_NIX_LOG"
        """,
    )
    verify_env = _storage_env(repo_root, str(work_dir / "bin"))
    verify_env["AIRLOCK_FAKE_NIX_LOG"] = str(result_dir / "nix-verify.args")
    _result, verify = _run_storage(
        repo_root,
        result_dir,
        "verify-command",
        [
            "admit",
            "--pool-root",
            str(work_dir / "verify-pool"),
            "--domain",
            "verify-command",
            "--system",
            f"/nix/store/{entries['common']}",
            "--closure-source",
            str(work_dir / "closures/work-gen1"),
            "--physical-store-root",
            str(physical_store),
            "--root-source",
            str(root_image),
            "--kernel-name",
            "kernel-a",
            "--verify-mode",
            "nix-store",
        ],
        env=verify_env,
    )
    assert verify["schema"] == "airlock.closure-store.admission.v1"
    assert verify["status"] == "passed"
    assert verify["verification"]["mode"] == "nix-store"
    assert "--extra-experimental-features" in verify["verification"]["command"]
    assert "nix-command" in (result_dir / "nix-verify.args").read_text().splitlines()

    fit_pool = work_dir / "fit-pool"
    _run_storage(
        repo_root,
        result_dir,
        "fit",
        [
            "admit",
            "--pool-root",
            str(fit_pool),
            "--domain",
            "fit",
            "--system",
            f"/nix/store/{entries['small_a']}",
            "--closure-source",
            str(work_dir / "closures/fit"),
            "--physical-store-root",
            str(physical_store),
            "--root-source",
            str(root_image),
            "--kernel-name",
            "kernel-a",
            "--verify-mode",
            "tree",
            "--segment-target-bytes",
            "1048576",
        ],
    )
    fit_segment = _object_segment(fit_pool, entries["small_a"])
    assert _object_segment(fit_pool, entries["small_b"]) == fit_segment
    assert _object_segment(fit_pool, entries["small_c"]) == fit_segment
    _result, fit_status = _run_storage(
        repo_root, result_dir, "fit-status", ["status", "--pool-root", str(fit_pool)]
    )
    assert fit_status["segment_count"] == 1
    assert fit_status["open_segment_count"] == 1

    pool = work_dir / "pool"
    _result, gen1 = _run_storage(
        repo_root,
        result_dir,
        "work-gen1",
        [
            "admit",
            "--pool-root",
            str(pool),
            "--domain",
            "work",
            "--system",
            f"/nix/store/{entries['work1']}",
            "--closure-source",
            str(work_dir / "closures/work-gen1"),
            "--physical-store-root",
            str(physical_store),
            "--root-source",
            str(root_image),
            "--kernel-name",
            "kernel-a",
            "--verify-mode",
            "tree",
            "--segment-target-bytes",
            "32768",
        ],
    )
    _result, gen2 = _run_storage(
        repo_root,
        result_dir,
        "work-gen2",
        [
            "admit",
            "--pool-root",
            str(pool),
            "--domain",
            "work",
            "--system",
            f"/nix/store/{entries['work2']}",
            "--closure-source",
            str(work_dir / "closures/work-gen2"),
            "--physical-store-root",
            str(physical_store),
            "--root-source",
            str(root_image),
            "--kernel-name",
            "kernel-a",
            "--verify-mode",
            "tree",
            "--segment-target-bytes",
            "32768",
        ],
    )
    assert gen1["generation"] == 1
    assert gen2["generation"] == 2
    assert gen2["previous_generation"] == 1
    _result, status = _run_storage(
        repo_root, result_dir, "status-gen2", ["status", "--pool-root", str(pool)]
    )
    assert status["schema"] == "airlock.closure-store.status.v1"
    assert status["domains"]["work"]["current"] == 2
    assert len(status["domains"]["work"]["generations"]) == 2
    _result, rollback = _run_storage(
        repo_root,
        result_dir,
        "rollback",
        ["rollback", "--pool-root", str(pool), "--domain", "work", "--generation", "1"],
    )
    assert rollback["status"] == "passed"
    assert rollback["current_generation"] == 1
    _result, switched = _run_storage(
        repo_root,
        result_dir,
        "switch",
        ["switch", "--pool-root", str(pool), "--domain", "work", "--generation", "2"],
    )
    assert switched["status"] == "passed"
    assert switched["current_generation"] == 2
    _run_storage(
        repo_root,
        result_dir,
        "retain-gen1-off",
        [
            "set-retention",
            "--pool-root",
            str(pool),
            "--domain",
            "work",
            "--generation",
            "1",
            "--retain",
            "false",
        ],
    )
    _result, gc = _run_storage(
        repo_root,
        result_dir,
        "gc",
        ["gc", "--pool-root", str(pool), "--keep-generations", "1"],
    )
    assert gc["schema"] == "airlock.closure-store.gc.v1"
    assert any(
        item["domain"] == "work" and item["generation"] == 1
        for item in gc["removed_generations"]
    )


def check_closure_store_failure_recovery(repo_root: Path, log_dir: Path) -> None:
    result_dir = log_dir
    work_dir = result_dir / "work"
    if work_dir.exists():
        _run(["chmod", "-R", "u+w", str(work_dir)], check=False)
    shutil.rmtree(result_dir, ignore_errors=True)
    (work_dir / "physical-store").mkdir(parents=True)
    (work_dir / "closures").mkdir()
    (work_dir / "bin").mkdir()
    result_dir.mkdir(parents=True, exist_ok=True)
    physical_store = work_dir / "physical-store"
    entries = {
        "common": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-common",
        "one": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-one",
        "two": "cccccccccccccccccccccccccccccccc-two",
        "three": "dddddddddddddddddddddddddddddddd-three",
        "four": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-four",
        "five": "ffffffffffffffffffffffffffffffff-five",
        "six": "11111111111111111111111111111111-six",
        "seven": "22222222222222222222222222222222-seven",
    }
    for entry in entries.values():
        _make_storage_object(physical_store, entry, entry)
    for name in ("one", "two", "three", "four", "five"):
        _make_storage_closure(work_dir, name, [entries[name]])
    _make_storage_closure(work_dir, "six", [entries["common"], entries["six"]])
    _make_storage_closure(work_dir, "seven", [entries["common"], entries["seven"]])
    _write_executable(
        work_dir / "bin/nix",
        """
        #!/usr/bin/env bash
        printf '%s\n' "$@" > "$AIRLOCK_FAKE_NIX_LOG"
        """,
    )
    verify_env = _storage_env(repo_root, str(work_dir / "bin"))
    verify_env["AIRLOCK_FAKE_NIX_LOG"] = str(result_dir / "nix-verify.args")
    verify_pool = work_dir / "verify-pool"
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "verification-before-command",
        "verification-before-command",
        [
            "admit",
            "--pool-root",
            str(verify_pool),
            "--domain",
            "verify",
            "--system",
            f"/nix/store/{entries['one']}",
            "--closure-source",
            str(work_dir / "closures/one"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "nix-store",
        ],
        env=verify_env,
    )
    _run_storage(
        repo_root,
        result_dir,
        "recover-verify",
        ["recover", "--pool-root", str(verify_pool)],
    )
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "verify-status",
        ["status", "--pool-root", str(verify_pool)],
    )
    assert status["domains"].get("verify") is None

    append_pool = work_dir / "append-pool"
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "object-after-append",
        "object-after-append",
        [
            "admit",
            "--pool-root",
            str(append_pool),
            "--domain",
            "append",
            "--system",
            f"/nix/store/{entries['one']}",
            "--closure-source",
            str(work_dir / "closures/one"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
        ],
    )
    assert not (append_pool / f"objects/blob-metadata/{entries['one']}.json").exists()
    assert any((append_pool / "objects/segments").glob("*.blob"))
    _run_storage(
        repo_root,
        result_dir,
        "recover-append",
        ["recover", "--pool-root", str(append_pool)],
    )
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "append-status",
        ["status", "--pool-root", str(append_pool)],
    )
    assert status["domains"].get("append") is None

    metadata_pool = work_dir / "metadata-pool"
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "object-after-metadata",
        "object-after-metadata",
        [
            "admit",
            "--pool-root",
            str(metadata_pool),
            "--domain",
            "metadata",
            "--system",
            f"/nix/store/{entries['two']}",
            "--closure-source",
            str(work_dir / "closures/two"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
        ],
    )
    assert (metadata_pool / f"objects/blob-metadata/{entries['two']}.json").is_file()
    _result, gc_active = _run_storage(
        repo_root,
        result_dir,
        "gc-active-metadata",
        ["gc", "--pool-root", str(metadata_pool), "--keep-generations", "0"],
    )
    assert entries["two"] in gc_active["active_transaction_objects"]
    assert entries["two"] not in gc_active["removed_object_blobs"]
    _run_storage(
        repo_root,
        result_dir,
        "recover-metadata",
        ["recover", "--pool-root", str(metadata_pool)],
    )
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "metadata-status",
        ["status", "--pool-root", str(metadata_pool)],
    )
    assert status["domains"].get("metadata") is None
    _result, gc_aborted = _run_storage(
        repo_root,
        result_dir,
        "gc-aborted-metadata",
        ["gc", "--pool-root", str(metadata_pool), "--keep-generations", "0"],
    )
    assert entries["two"] in gc_aborted["removed_object_blobs"]

    finalize_pool = work_dir / "finalize-pool"
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "generation-after-tx-commit",
        "generation-after-tx-commit",
        [
            "admit",
            "--pool-root",
            str(finalize_pool),
            "--domain",
            "finalize",
            "--system",
            f"/nix/store/{entries['five']}",
            "--closure-source",
            str(work_dir / "closures/five"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
            "--activate",
            "false",
        ],
    )
    manifest = json.loads(
        (finalize_pool / "domains/finalize/generations/1/manifest.json").read_text()
    )
    assert manifest["status"] == "prepared"
    _result, gc_before = _run_storage(
        repo_root,
        result_dir,
        "gc-finalize-before-recover",
        ["gc", "--pool-root", str(finalize_pool), "--keep-generations", "0"],
    )
    assert any(
        item["domain"] == "finalize"
        and item["generation"] == 1
        and item["transaction_status"] == "committed"
        for item in gc_before["in_progress_generations"]
    )
    _result, recover_finalize = _run_storage(
        repo_root,
        result_dir,
        "recover-finalize",
        ["recover", "--pool-root", str(finalize_pool)],
    )
    assert len(recover_finalize["finalized_generations"]) == 1
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "status-finalize-after",
        ["status", "--pool-root", str(finalize_pool)],
    )
    assert status["domains"]["finalize"]["current"] is None
    assert status["domains"]["finalize"]["generations"][0]["generation"] == 1

    switch_pool = work_dir / "switch-pool"
    _run_storage(
        repo_root,
        result_dir,
        "switch-gen1",
        [
            "admit",
            "--pool-root",
            str(switch_pool),
            "--domain",
            "switch",
            "--system",
            f"/nix/store/{entries['six']}",
            "--closure-source",
            str(work_dir / "closures/six"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
        ],
    )
    _run_storage(
        repo_root,
        result_dir,
        "switch-gen2",
        [
            "admit",
            "--pool-root",
            str(switch_pool),
            "--domain",
            "switch",
            "--system",
            f"/nix/store/{entries['seven']}",
            "--closure-source",
            str(work_dir / "closures/seven"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
            "--activate",
            "false",
        ],
    )
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "switch-before-current-replace",
        "switch-before-current-replace",
        [
            "switch",
            "--pool-root",
            str(switch_pool),
            "--domain",
            "switch",
            "--generation",
            "2",
        ],
    )
    _run_storage(
        repo_root,
        result_dir,
        "recover-switch-before",
        ["recover", "--pool-root", str(switch_pool)],
    )
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "status-switch-before",
        ["status", "--pool-root", str(switch_pool)],
    )
    assert status["domains"]["switch"]["current"] == 1
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "switch-after-current-replace",
        "switch-after-current-replace",
        [
            "switch",
            "--pool-root",
            str(switch_pool),
            "--domain",
            "switch",
            "--generation",
            "2",
        ],
    )
    _run_storage(
        repo_root,
        result_dir,
        "recover-switch-after",
        ["recover", "--pool-root", str(switch_pool)],
    )
    _result, status = _run_storage(
        repo_root,
        result_dir,
        "status-switch-after",
        ["status", "--pool-root", str(switch_pool)],
    )
    assert status["domains"]["switch"]["current"] == 2

    gc_pool = work_dir / "gc-pool"
    _run_storage(
        repo_root,
        result_dir,
        "gc-gen1",
        [
            "admit",
            "--pool-root",
            str(gc_pool),
            "--domain",
            "gc-domain",
            "--system",
            f"/nix/store/{entries['common']}",
            "--closure-source",
            str(work_dir / "closures/six"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
        ],
    )
    _run_storage(
        repo_root,
        result_dir,
        "gc-gen2",
        [
            "admit",
            "--pool-root",
            str(gc_pool),
            "--domain",
            "gc-domain",
            "--system",
            f"/nix/store/{entries['seven']}",
            "--closure-source",
            str(work_dir / "closures/seven"),
            "--physical-store-root",
            str(physical_store),
            "--verify-mode",
            "tree",
            "--retain",
            "false",
        ],
    )
    _run_storage(
        repo_root,
        result_dir,
        "gc-rollback",
        [
            "rollback",
            "--pool-root",
            str(gc_pool),
            "--domain",
            "gc-domain",
            "--generation",
            "1",
        ],
    )
    _run_storage_failpoint(
        repo_root,
        result_dir,
        "gc-after-object-metadata-remove",
        "gc-after-object-metadata-remove",
        ["gc", "--pool-root", str(gc_pool), "--keep-generations", "0"],
    )
    _run_storage(
        repo_root, result_dir, "recover-gc", ["recover", "--pool-root", str(gc_pool)]
    )
    _result, status = _run_storage(
        repo_root, result_dir, "status-gc", ["status", "--pool-root", str(gc_pool)]
    )
    assert status["domains"]["gc-domain"]["current"] == 1
    assert len(status["domains"]["gc-domain"]["generations"]) == 1
