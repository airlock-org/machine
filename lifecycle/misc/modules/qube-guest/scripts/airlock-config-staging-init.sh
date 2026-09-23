set -eu

repo="${AIRLOCK_CONFIG_REPO_PATH:-@NIX_0@}"
config_inputs_root="${AIRLOCK_CONFIG_INPUTS_PATH:-@NIX_1@}"

config_inputs_ready() {
  [ -d "$config_inputs_root/nixpkgs" ] &&
    [ -d "$config_inputs_root/nixpkgs-libvirt_10_5" ] &&
    [ -d "$config_inputs_root/nixos-qubes" ] &&
    [ -d "$config_inputs_root/airlock-support" ]
}

@NIX_2@
@NIX_3@

config_inputs_need_refresh() {
  for input_file in flake.nix flake.lock; do
    [ -f "$input_file" ] || continue
    @NIX_4@/bin/grep -F '__AIRLOCK_NIXPKGS_URL__' "$input_file" >/dev/null 2>&1 && return 0
    @NIX_4@/bin/grep -F '/rw/airlock/config-inputs/' "$input_file" >/dev/null 2>&1 && return 0
    @NIX_4@/bin/grep -F '/rw/home/@NIX_5@/.local/share/airlock/config-inputs/' "$input_file" >/dev/null 2>&1 && return 0
    @NIX_4@/bin/grep -F '@NIX_6@/' "$input_file" >/dev/null 2>&1 && return 0
  done
  return 1
}

require_clean_worktree_for_input_update() {
  dirty="$(@NIX_7@/bin/git status --porcelain)"
  if [ -n "$dirty" ]; then
    printf 'config staging worktree is dirty; refusing to refresh config input paths\n%s\n' "$dirty" >&2
    exit 1
  fi
}

add_config_input_lock_if_ready() {
  if [ -f flake.lock ]; then
    printf 'config staging flake.lock already exists: %s\n' "$repo/flake.lock"
    return 0
  fi

  if ! config_inputs_ready; then
    printf 'config staging config inputs are not present yet under %s; flake.lock deferred\n' "$config_inputs_root"
    return 0
  fi

  substitute_config_inputs "$config_inputs_root"
  @NIX_8@/bin/airlock-config-nix \
    --extra-experimental-features nix-command \
    --extra-experimental-features flakes \
    flake lock \
    --impure \
    --offline
  @NIX_7@/bin/git add flake.nix flake.lock
}

if [ -d "$repo/.git" ]; then
  cd "$repo"
  if [ "${AIRLOCK_CONFIG_REFRESH_INPUTS:-0}" = 1 ]; then
    if ! config_inputs_ready; then
      printf 'config staging config inputs are not present yet under %s; refresh deferred\n' "$config_inputs_root"
      printf 'config staging repo already exists: %s\n' "$repo"
      exit 0
    fi
    dirty="$(@NIX_7@/bin/git status --porcelain)"
    if [ -n "$dirty" ]; then
      printf 'config staging config input refresh deferred because worktree is dirty\n%s\n' "$dirty"
    else
      substitute_config_inputs "$config_inputs_root"
      @NIX_8@/bin/airlock-config-nix \
        --extra-experimental-features nix-command \
        --extra-experimental-features flakes \
        flake lock \
        --recreate-lock-file \
        --impure \
        --offline
      if [ -n "$(@NIX_7@/bin/git status --porcelain flake.nix flake.lock)" ]; then
        @NIX_7@/bin/git add flake.nix flake.lock
        @NIX_7@/bin/git \
          -c user.name='Airlock Config Staging' \
          -c user.email='airlock-config-staging@localhost' \
          commit -m 'refresh airlock config inputs'
      elif config_inputs_need_refresh; then
        printf 'config staging config input paths still need refresh after lock update\n' >&2
        exit 1
      else
        printf 'config staging config input lock is current\n'
      fi
    fi
  elif config_inputs_need_refresh; then
    printf 'config staging config input refresh is required but deferred for existing repo: %s\n' "$repo"
  fi
  if [ ! -f flake.lock ]; then
    require_clean_worktree_for_input_update
    add_config_input_lock_if_ready
    if ! @NIX_7@/bin/git diff --cached --quiet; then
      @NIX_7@/bin/git \
        -c user.name='Airlock Config Staging' \
        -c user.email='airlock-config-staging@localhost' \
        commit -m 'lock airlock config candidate inputs'
    fi
  fi
  printf 'config staging repo already exists: %s\n' "$repo"
  exit 0
fi

if [ "${AIRLOCK_CONFIG_STAGING_INIT_FROM_INITIAL_CONFIG:-0}" != 1 ]; then
  @NIX_9@/bin/mkdir -p "$repo"
  if [ -n "$(@NIX_10@/bin/find "$repo" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
    printf 'config staging path exists but is not a Git repo; waiting for sys-config-vault current import: %s\n' "$repo"
  else
    printf 'config staging repo is empty; waiting for sys-config-vault current import: %s\n' "$repo"
  fi
  exit 0
fi

mkdir -p "$repo"
cd "$repo"
@NIX_7@/bin/git init -b main
write_initial_runtime_config_repo_files
substitute_config_inputs "$config_inputs_root"
@NIX_7@/bin/git add @NIX_11@
add_config_input_lock_if_ready
@NIX_7@/bin/git \
  -c user.name='Airlock Config Staging' \
  -c user.email='airlock-config-staging@localhost' \
  commit -m 'init airlock config candidate'
