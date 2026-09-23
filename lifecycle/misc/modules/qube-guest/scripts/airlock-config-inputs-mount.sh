set -eu

target_root=@NIX_0@
store_root=@NIX_1@
public_root=@NIX_2@
manifest="$target_root/manifest.json"

mkdir -p "$target_root" "$public_root"
[ -f "$manifest" ] || exit 0

jq -r '.inputs | to_entries[] | [.key, .value] | @tsv' "$manifest" |
  while IFS="$(printf '\t')" read -r name input_path; do
    case "$name" in
      nixpkgs|nixpkgs-libvirt_10_5|nixos-qubes|airlock-support) ;;
      *)
        printf 'invalid config input name: %s\n' "$name" >&2
        exit 1
        ;;
    esac
    input_store_path="$(printf '%s\n' "$input_path" | sed 's|^\(/nix/store/[^/]*\).*|\1|')"
    input_relative_path="${input_path#"$input_store_path"}"
    physical_path="$store_root$input_store_path$input_relative_path"
    [ -d "$physical_path" ] || {
      printf 'imported config input path is missing from private Nix store: %s\n' "$physical_path" >&2
      exit 1
    }
    mountpoint_path="$public_root/$name"
    if mountpoint -q "$mountpoint_path"; then
      continue
    fi
    if [ -e "$mountpoint_path" ] && [ ! -d "$mountpoint_path" ]; then
      rm -rf "$mountpoint_path"
    fi
    mkdir -p "$mountpoint_path"
    mount --bind "$physical_path" "$mountpoint_path"
  done
