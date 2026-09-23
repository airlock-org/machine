set -eu

expected_role=@NIX_0@
vm_name="$(qubesdb-read /name 2>/dev/null || true)"
qdb_role="$(qubesdb-read /features/airlock-role 2>/dev/null || true)"

case "$qdb_role" in
  "") ;;
  "$expected_role") ;;
  sys-config-vault|sys-config-staging|sys-config-validator)
    echo "airlock config guest init: QubesDB role mismatch: expected=$expected_role actual=$qdb_role vm=$vm_name" >&2
    exit 1
    ;;
  *)
    echo "airlock config guest init: unsupported QubesDB role: expected=$expected_role actual=$qdb_role vm=$vm_name" >&2
    exit 1
    ;;
esac

case "$vm_name" in
  ""|"$expected_role") ;;
  sys-config-vault|sys-config-staging|sys-config-validator)
    echo "airlock config guest init: VM name mismatch: expected=$expected_role actual=$vm_name qdb_role=$qdb_role" >&2
    exit 1
    ;;
  *) ;;
esac

run_as_user() {
  @NIX_1@/bin/runuser -u @NIX_2@ -- \
    @NIX_3@/bin/env \
      HOME=/home/@NIX_2@ \
      USER=@NIX_2@ \
      LOGNAME=@NIX_2@ \
      XDG_CACHE_HOME=/home/@NIX_2@/.cache \
      PATH=@NIX_4@ \
      "$@"
}

run_as_user @NIX_5@
