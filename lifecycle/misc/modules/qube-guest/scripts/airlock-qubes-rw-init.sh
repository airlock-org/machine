set -eu

mkdir -p /rw /home /usr/local
mkdir -p /var/lib/qubes

if [ ! -e /dev/xvdb ]; then
  echo "Qubes private device /dev/xvdb is not present" >&2
  exit 1
fi

@NIX_0@/lib/qubes/init/setup-rwdev.sh

if ! mountpoint -q /rw; then
  mount -t auto -o defaults,discard,nosuid,nodev /dev/xvdb /rw
fi

@NIX_0@/lib/qubes/init/setup-rw.sh

. @NIX_0@/lib/qubes/init/functions
initialize_home /rw/home ifneeded

if ! mountpoint -q /home; then
  mount --bind /rw/home /home
  mount -o remount,bind,nosuid,nodev /home
fi

install -d -m 0700 -o @NIX_1@ -g users /home/@NIX_1@

mkdir -p /rw/usrlocal
if ! mountpoint -q /usr/local; then
  mount --bind /rw/usrlocal /usr/local
fi
