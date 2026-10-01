#!/usr/bin/env bash
set -eu
[ "${QREXEC_REMOTE_DOMAIN:-}" = sys-gui ] || exit 1
[ -z "${QREXEC_SERVICE_ARGUMENT:-}" ] || exit 1

printf 'Checking the Internet connection from sys-agent...\n'
if [ -z "$(ip -4 route show default)" ]; then
  echo 'No network route in sys-agent. Check sys-net and sys-firewall, then retry.' >&2
  exit 1
fi
if ! timeout --kill-after=2s 5s getent ahostsv4 controlplane.tailscale.com >/dev/null; then
  echo 'DNS is not working from sys-agent. Check the connection and DNS in sys-net and sys-firewall, then retry.' >&2
  exit 1
fi
if ! curl --silent --show-error --output /dev/null --connect-timeout 5 --max-time 15 https://controlplane.tailscale.com/; then
  echo 'Cannot reach Tailscale over HTTPS. Check the connection and firewall services in sys-net and sys-firewall, then retry.' >&2
  exit 1
fi
printf 'Connection available. Starting Tailscale login (two-minute limit)...\n'
if timeout --foreground --kill-after=5s 120s tailscale up --ssh; then
  exit 0
else
  status=$?
  if [ "$status" = 124 ]; then
    echo 'Tailscale login timed out. Check the connection and complete browser login, then retry.' >&2
  else
    echo 'Tailscale enrollment failed. See the error above, then retry.' >&2
  fi
  exit "$status"
fi
