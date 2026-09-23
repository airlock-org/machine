# Keep this close to upstream qubes.GetAppmenus, but use NixOS paths.
if [ -r /etc/profile ]; then
  # shellcheck disable=SC1091
  . /etc/profile
fi

export PATH=@NIX_0@:$PATH

if [ -z "${XDG_DATA_HOME:-}" ]; then
  qrexec_user="$(whoami)"
  if [ "$qrexec_user" = root ]; then
    qrexec_user="$(qubesdb-read /default-user 2>/dev/null || printf '%s\n' @NIX_1@)"
  fi

  home="$(getent passwd "$qrexec_user" | cut -d: -f6)"
  if [ -z "$home" ]; then
    home="/home/$qrexec_user"
  fi
  XDG_DATA_HOME="$home/.local/share"
fi

if [ -z "${XDG_DATA_DIRS:-}" ]; then
  XDG_DATA_DIRS="/run/current-system/sw/share:/usr/local/share:/usr/share"
fi

persistence="$(qubesdb-read /qubes-vm-persistence 2>/dev/null || printf 'full\n')"
rw_devno=""
if [ -e /rw ]; then
  rw_devno="$(stat -c %D /rw 2>/dev/null || true)"
fi

apps_dirs_to_consider=( "$XDG_DATA_HOME" )
old_IFS="$IFS"
IFS=:
# shellcheck disable=SC2206
apps_dirs_to_consider+=( $XDG_DATA_DIRS )
IFS="$old_IFS"

apps_dirs=()
for dir in "${apps_dirs_to_consider[@]}"; do
  case "$dir" in
    /*) ;;
    *) continue ;;
  esac

  if [ "$persistence" = full ]; then
    apps_dirs+=( "$dir/applications" )
  elif [ "$persistence" = rw-only ] &&
    [ -n "$rw_devno" ] &&
    [ -e "$dir" ] &&
    [ "$(stat -c %D "$dir" 2>/dev/null || true)" = "$rw_devno" ]; then
    apps_dirs+=( "$dir/applications" )
  fi
done

for app_dir in "${apps_dirs[@]}"; do
  case "$app_dir" in
    /*) ;;
    *) continue ;;
  esac

  [ -d "$app_dir" ] || continue

  (
    cd "$app_dir"
    find . -name '*.desktop' -exec awk -b '
      BEGINFILE { if (ERRNO) nextfile; entry=""; file = substr(FILENAME, 3); gsub(/\//, "-", file) }
      /^\[/ { if (tolower($0) != "\[desktop entry\]") nextfile }
      /^Exec *=/ { entry = entry file ":Exec=qubes-desktop-run " file "\n"; next }
      /^NoDisplay *= *true$/ { entry=""; nextfile }
      /^Categories *= *Screensaver;$/ { entry=""; nextfile }
      /=/ { entry = entry file ":" $0 "\n" }
      ENDFILE { print entry }
    ' 2>/dev/null '{}' +
  )
done
