#!/usr/bin/env bash
set -euo pipefail

codex_state="$1"
instructions="$2"
guest_home="$3"
home_instructions="$4"
repo="$5"

# Relocate the entire existing checkout, including local branches and edits,
# before first-boot source seeding can create a checkout at the new location.
old_repo="$guest_home/dev/airlock-machine"
if [ "$old_repo" != "$repo" ] && [ -e "$old_repo" ] && [ ! -L "$old_repo" ]; then
  if [ ! -e "$repo" ] && [ ! -L "$repo" ]; then
    [ -d "$old_repo/.git" ] || { echo "Not a Git checkout: $old_repo" >&2; exit 1; }
    mv -T -- "$old_repo" "$repo"
  else
    echo "Preserving both existing checkout paths: $old_repo and $repo" >&2
  fi
fi

if [ ! -e "$guest_home/AGENTS.md" ] && [ ! -L "$guest_home/AGENTS.md" ]; then
  ln -s "$home_instructions" "$guest_home/AGENTS.md"
fi
# Retarget only the exact managed links copied from the previous VM. User
# files, custom links and deliberate overrides keep their original contents.
if [ -L "$guest_home/AGENTS.md" ] && [ "$(readlink -- "$guest_home/AGENTS.md")" = /etc/airlock/sys-agent/home-AGENTS.md ]; then
  ln -sfn -- "$home_instructions" "$guest_home/AGENTS.md"
fi
install -d -m 0700 "$codex_state"
# A stable /etc link follows system generations. Preserve user-written guidance
# and deliberate overrides, including an existing dangling symlink.
if [ ! -e "$codex_state/AGENTS.md" ] && [ ! -L "$codex_state/AGENTS.md" ]; then
  ln -s "$instructions" "$codex_state/AGENTS.md"
fi
if [ -L "$codex_state/AGENTS.md" ] && [ "$(readlink -- "$codex_state/AGENTS.md")" = /etc/airlock/sys-agent/AGENTS.md ]; then
  ln -sfn -- "$instructions" "$codex_state/AGENTS.md"
fi
