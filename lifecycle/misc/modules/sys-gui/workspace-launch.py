"""Prepare the desktop's VM workspace before the common application launcher runs."""

import ast
import subprocess
import sys
import uuid

import qubesadmin


def main():
    operation, value = sys.argv[1:]
    if operation == "prepare":
        method = "Prepare"
        value = str(qubesadmin.Qubes().domains[value].uuid)
    elif operation == "cancel":
        method = "Cancel"
    else:
        raise ValueError("Expected prepare VM or cancel TOKEN")
    result = subprocess.run(
        [
            "@gdbus@", "call", "--session", "--timeout", "5",
            "--dest", "org.airlock.Workspaces",
            "--object-path", "/org/airlock/Workspaces",
            "--method", f"org.airlock.Workspaces.{method}",
            str(uuid.UUID(value)),
        ],
        text=True, capture_output=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "The desktop workspace service is unavailable")
    if operation == "prepare":
        token, = ast.literal_eval(result.stdout)
        print(str(uuid.UUID(token)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.SubprocessError, qubesadmin.exc.QubesException) as error:
        print(f"Workspace preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
