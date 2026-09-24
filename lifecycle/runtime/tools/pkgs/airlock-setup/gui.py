#!/usr/bin/env python3
"""First-run AirlockOS welcome window in sys-gui."""

import os
import subprocess
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

PREVIEW = os.environ.get("AIRLOCK_SETUP_PREVIEW") == "1"
preview_complete = False


def rpc(operation):
    global preview_complete
    if PREVIEW:
        if operation == "airlock.SetupComplete":
            preview_complete = True
        return "complete" if preview_complete else "incomplete"
    result = subprocess.run(
        ["qvm-run", "--pass-io", "--no-gui", "--service", "dom0", operation],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        action = "save setup completion" if operation == "airlock.SetupComplete" else "read setup status"
        raise RuntimeError(f"Could not {action}. Please try again.")
    state = result.stdout.strip()
    if state not in ("complete", "incomplete"):
        raise RuntimeError("Unexpected setup state response")
    return state


class Setup(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="org.airlockos.SetupPreview" if PREVIEW else "org.airlockos.Setup",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window = None
        self.dismissed = False
        self.held = False

    def do_command_line(self, command_line):
        automatic = "--auto" in command_line.get_arguments()[1:]
        if automatic and self.dismissed:
            return 0
        try:
            state = rpc("airlock.SetupStatus")
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            state = None
            error = str(exc)
        if automatic and state == "complete":
            return 0
        self.show_window(state, error if state is None else None)
        return 0

    def show_window(self, state, error):
        if self.window is not None:
            self.window.present()
            return
        if not self.held:
            self.hold()  # Keep dismissal in memory until the desktop session ends.
            self.held = True
        self.window = Adw.ApplicationWindow(application=self, title="AirlockOS Setup Preview" if PREVIEW else "AirlockOS Setup")
        self.window.set_default_size(620, 440)
        self.window.connect("close-request", self.on_close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.page = Adw.StatusPage(icon_name="preferences-system-symbolic")
        self.page.set_title("Welcome to AirlockOS")
        self.page.set_description("Your desktop is ready. Finish setup to stop showing this welcome screen when you sign in. You can reopen Setup from the applications menu.")
        toolbar.set_content(self.page)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_margin_top(12)
        actions.set_margin_bottom(12)
        actions.set_margin_start(18)
        actions.set_margin_end(18)
        actions.set_halign(Gtk.Align.END)
        self.message = Gtk.Label(wrap=True, selectable=True)
        self.message.set_hexpand(True)
        self.message.set_halign(Gtk.Align.START)
        actions.append(self.message)
        self.retry = Gtk.Button(label="Retry")
        self.retry.connect("clicked", self.on_retry)
        actions.append(self.retry)
        self.finish = Gtk.Button(label="Finish setup")
        self.finish.add_css_class("suggested-action")
        self.finish.connect("clicked", self.on_finish)
        actions.append(self.finish)
        toolbar.add_bottom_bar(actions)
        self.window.set_content(toolbar)
        self.update(state, error)
        self.window.present()

    def update(self, state, error):
        if state == "complete":
            self.page.set_title("Setup complete")
            self.page.set_description("AirlockOS is ready. You can reopen Setup from the applications menu.")
        else:
            self.page.set_title("Welcome to AirlockOS")
            self.page.set_description("Your desktop is ready. Finish setup to stop showing this welcome screen when you sign in. You can reopen Setup from the applications menu.")
        self.message.set_text(error or ("Setup is already complete." if state == "complete" else ""))
        self.retry.set_visible(error is not None)
        self.finish.set_sensitive(state == "incomplete")
        self.finish.set_visible(state != "complete")

    def on_retry(self, _button):
        try:
            state = rpc("airlock.SetupStatus")
            self.update(state, None)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            self.update(None, str(exc))

    def on_finish(self, _button):
        self.finish.set_sensitive(False)
        try:
            rpc("airlock.SetupComplete")
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            self.update(None, str(exc))
            return
        self.update("complete", None)

    def on_close(self, _window):
        self.dismissed = True
        self.window = None
        return False


if __name__ == "__main__":
    sys.exit(Setup().run(sys.argv))
