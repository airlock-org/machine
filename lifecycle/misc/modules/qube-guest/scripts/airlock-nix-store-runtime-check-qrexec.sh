set -eu

export PATH=@NIX_0@

system_path="$(
  tr ' ' '\n' < /proc/cmdline |
    sed -n 's|^airlock.system=||p' |
    head -n 1
)"
store_device="$(
  tr ' ' '\n' < /proc/cmdline |
    sed -n 's|^airlock.storedev=||p' |
    head -n 1
)"
store_format="$(
  tr ' ' '\n' < /proc/cmdline |
    sed -n 's|^airlock.storefmt=||p' |
    head -n 1
)"
store_blob_device="$(
  tr ' ' '\n' < /proc/cmdline |
    sed -n 's|^airlock.storeblobdev=||p' |
    head -n 1
)"
[ -n "$store_format" ] || store_format="erofs"

[ -n "$system_path" ] || {
  echo "missing airlock.system kernel option" >&2
  exit 1
}
[ -n "$store_device" ] || {
  echo "missing airlock.storedev kernel option" >&2
  exit 1
}
[ -x "$system_path/init" ] || {
  echo "system init is not executable: $system_path/init" >&2
  exit 1
}

current_system="$(readlink -f /run/current-system)"
[ "$current_system" = "$system_path" ] || {
  echo "current system mismatch: $current_system != $system_path" >&2
  exit 1
}

mount_source="$(findmnt -rn -T /nix/store -o SOURCE)"
mount_options="$(findmnt -rn -T /nix/store -o OPTIONS)"

[ "$mount_source" = "$store_device" ] || {
  echo "/nix/store source mismatch: $mount_source != $store_device" >&2
  exit 1
}
if [ "$store_format" = "erofs" ] && [ -n "$store_blob_device" ]; then
  [ -b "$store_blob_device" ] || {
    echo "store blob device is missing: $store_blob_device" >&2
    exit 1
  }
fi
case ",$mount_options," in
  *,ro,*) ;;
  *)
    echo "/nix/store is not mounted read-only: $mount_options" >&2
    exit 1
    ;;
esac

write_probe="/nix/store/.airlock-runtime-write-probe-$$"
if touch "$write_probe" 2>/tmp/airlock-runtime-write-probe.err; then
  rm -f "$write_probe"
  echo "/nix/store accepted a write probe" >&2
  exit 1
fi

printf 'system=%s\n' "$system_path"
printf 'store_device=%s\n' "$store_device"
printf 'store_format=%s\n' "$store_format"
printf 'store_blob_device=%s\n' "$store_blob_device"
printf 'store_options=%s\n' "$mount_options"
