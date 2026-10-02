#!/usr/bin/env bash
set -euo pipefail

codex_state="$1"
instructions="$2"
install -d -m 0700 "$codex_state"
# A stable /etc link follows system generations. Preserve user-written guidance
# and deliberate overrides, including an existing dangling symlink.
if [ ! -e "$codex_state/AGENTS.md" ] && [ ! -L "$codex_state/AGENTS.md" ]; then
  ln -s "$instructions" "$codex_state/AGENTS.md"
fi
