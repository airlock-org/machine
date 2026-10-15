from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path


def _command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip()


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdout: Path | None = None,
    stderr: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    stdout_handle = stdout.open("w", encoding="utf-8") if stdout is not None else None
    stderr_handle = None
    if stderr is not None and stderr == stdout:
        stderr_handle = stdout_handle
    elif stderr is not None:
        stderr_handle = stderr.open("w", encoding="utf-8")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
            stderr=stderr_handle if stderr_handle is not None else subprocess.PIPE,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None and stderr_handle is not stdout_handle:
            stderr_handle.close()
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed with exit {result.returncode}: {' '.join(command)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _run_store_path(
    repo_root: Path,
    store_path: str,
    env: dict[str, str],
    args: list[str] | None = None,
) -> None:
    _run(
        [
            "bash",
            "-c",
            'source "$1"; airlock_run_store_path "$2" "${@:3}"',
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/lib/airlock-env.sh"),
            store_path,
            *(args or []),
        ],
        cwd=repo_root,
        env=env,
    )


def _physical_store_path(repo_root: Path, path: str) -> str:
    return _run(
        [
            "bash",
            "-c",
            'source "$1"; airlock_physical_store_path "$2"',
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/lib/airlock-env.sh"),
            path,
        ],
        cwd=repo_root,
    ).stdout.strip()


def _physical_path_list(repo_root: Path, path_list: str) -> str:
    return _run(
        [
            "bash",
            "-c",
            'source "$1"; airlock_physical_path_list "$2"',
            "bash",
            str(repo_root / "lifecycle/misc/tools/scripts/lib/airlock-env.sh"),
            path_list,
        ],
        cwd=repo_root,
    ).stdout.strip()


def _resolve_physical_store_path(
    repo_root: Path, path: Path, *, resolve_final: bool = True
) -> Path:
    """Resolve a path whose symlinks may target the virtual ``/nix/store``."""
    resolved = Path(path.anchor)
    pending = list(path.parts[1:])
    seen: set[Path] = set()

    while pending:
        candidate = resolved / pending.pop(0)
        if not candidate.is_symlink() or (not resolve_final and not pending):
            resolved = candidate
            continue

        if candidate in seen:
            raise RuntimeError(f"symlink loop while resolving {path}: {candidate}")
        seen.add(candidate)

        target = Path(os.readlink(candidate))
        if target.is_absolute():
            target = Path(_physical_store_path(repo_root, str(target)))
        else:
            target = resolved / target
        resolved = Path(target.anchor)
        pending = list(target.parts[1:]) + pending

    return resolved


def _file_contains_word(path: Path, word: str) -> bool:
    if not path.is_file():
        return False
    return word in path.read_text(encoding="utf-8", errors="replace").split()


def _first_cpu_flags() -> set[str]:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.is_file():
        return set()
    for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("flags", "Features")) and ":" in line:
            return set(line.split(":", 1)[1].split())
    return set()


def _first_readable(paths: list[Path]) -> str:
    for path in paths:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def _write_flake(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _setup_fake_command_dir(bin_dir: Path, commands: dict[str, str]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, script in commands.items():
        _write_executable(bin_dir / name, script)


def _git_commit_all(repo: Path, message: str, *, name: str, email: str) -> str:
    _run(["git", "init", "-b", "main"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(
        [
            "git",
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "-m",
            message,
        ],
        cwd=repo,
    )
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _nix_eval_json(repo_root: Path, attr: str, *extra: str) -> object:
    return json.loads(
        _run(
            ["nix", "eval", "--json", *extra, f"{repo_root}#{attr}"], cwd=repo_root
        ).stdout.strip()
    )


def _nix_eval_raw(repo_root: Path, attr: str, *extra: str) -> str:
    return _run(
        ["nix", "eval", "--raw", *extra, f"{repo_root}#{attr}"], cwd=repo_root
    ).stdout
