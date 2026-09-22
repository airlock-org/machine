#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN="${1:?usage: airlock-start-qube-ready DOMAIN [TOPOLOGY]}"
TOPOLOGY="${2:-${AIRLOCK_QUBES_TOPOLOGY:-/var/lib/airlock/system/current-topology.json}}"
boot_id="$(tr -d '\n' </proc/sys/kernel/random/boot_id)"
LOG_DIR="${AIRLOCK_BOOT_ATTEMPT_LOG_DIR:-/var/log/airlock/boot-attempts/$boot_id}"
START_TIMEOUT="${AIRLOCK_QUBE_START_TIMEOUT:-240}"
READY_TIMEOUT="${AIRLOCK_QUBE_READY_TIMEOUT:-180}"
QVM_START="${AIRLOCK_QVM_START:-qvm-start}"
QVM_CHECK="${AIRLOCK_QVM_CHECK:-qvm-check}"
QVM_LS="${AIRLOCK_QVM_LS:-qvm-ls}"
QREXEC_CLIENT="${AIRLOCK_QREXEC_CLIENT:-qrexec-client}"
SUPERVISOR="${AIRLOCK_BOOT_SUPERVISOR:-airlock-boot-supervisor}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/start-$DOMAIN.log"
expected="$(jq -er --arg domain "$DOMAIN" '.domains[$domain].backend.system.source' "$TOPOLOGY")"

fail() {
  local reason="$1"
  printf '%s\n' "$reason" >&2
  "$SUPERVISOR" fail "qube:$DOMAIN" "$reason" || true
  exit 1
}
trap 'fail "$DOMAIN readiness command failed at line $LINENO"' ERR

"$SUPERVISOR" stage "qube:$DOMAIN" "starting $DOMAIN; expecting $expected"
{
  printf 'domain=%s expected=%s start_timeout=%s ready_timeout=%s\n' \
    "$DOMAIN" "$expected" "$START_TIMEOUT" "$READY_TIMEOUT"
  printf '+ qvm-start --skip-if-running -- %s\n' "$DOMAIN"
  timeout --foreground --signal=TERM --kill-after=10s "${START_TIMEOUT}s" \
    "$QVM_START" --skip-if-running -- "$DOMAIN"

  deadline=$((SECONDS + READY_TIMEOUT))
  attempt=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    attempt=$((attempt + 1))
    printf 'readiness attempt=%s elapsed=%ss\n' "$attempt" "$SECONDS"
    "$QVM_CHECK" --running "$DOMAIN" || true
    "$QVM_LS" --raw-data --fields NAME,STATE,XID "$DOMAIN" || true
    actual="$(timeout --foreground --signal=TERM --kill-after=2s 15s \
      "$QREXEC_CLIENT" -d "$DOMAIN" 'user:readlink -f /run/current-system' 2>>"$LOG")" || {
        printf 'qrexec not ready: %s\n' "$actual"
        sleep 3
        continue
      }
    actual="${actual%$'\n'}"
    printf 'qrexec current-system=%s\n' "$actual"
    [ "$actual" = "$expected" ] || fail "$DOMAIN reported $actual; expected $expected"
    "$QVM_CHECK" --running "$DOMAIN"
    "$SUPERVISOR" stage "qube:$DOMAIN" "$DOMAIN is qrexec-ready on $expected"
    trap - ERR
    exit 0
  done
  fail "$DOMAIN did not become qrexec-ready within ${READY_TIMEOUT}s"
} >>"$LOG" 2>&1
