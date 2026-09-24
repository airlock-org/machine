"""Show GNOME's native notification for recoverable background startup failures."""

import json
import subprocess
import time


def rpc(service):
    return subprocess.run(
        ["qvm-run", "--pass-io", "--no-gui", "--service", "dom0", service],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout


def main():
    while True:
        try:
            state = json.loads(rpc("airlock.BootStatus"))
        except subprocess.CalledProcessError:
            # This desktop may be used on a profile without boot supervision.
            return
        except (subprocess.TimeoutExpired, ValueError):
            time.sleep(5)
            continue
        if state.get("status") == "complete":
            return
        if state.get("desktop_ready") and state.get("status") == "degraded":
            action = subprocess.run(
                [
                    "notify-send",
                    "--app-name=AirlockOS",
                    "--icon=dialog-warning",
                    "--expire-time=0",
                    "--wait",
                    "--action=retry=Retry",
                    "--action=dismiss=Dismiss",
                    "Some background services could not start",
                    "Your desktop is ready. You can retry starting the background services.",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if action != "retry":
                return
            try:
                rpc("airlock.BootRetry")
            except subprocess.SubprocessError:
                # The status remains degraded on failure, so Retry stays available.
                pass
        time.sleep(5)


if __name__ == "__main__":
    main()
