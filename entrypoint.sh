#!/bin/sh
set -e

# Load only the pixma backend (faster, no unrelated backends)
echo pixma > /etc/sane.d/dll.conf
rm -f /etc/sane.d/dll.d/* 2>/dev/null || true

# Configure the printer by IP (broadcast discovery stays active as a fallback).
# The MX925 sometimes takes >10 s (the default timeout) to answer on scan start.
if [ -n "$SCANNER_IP" ]; then
  echo "bjnp://$SCANNER_IP/timeout=${BJNP_TIMEOUT:-30000}" > /etc/sane.d/pixma.conf
fi

# Name shown on the printer's panel: the pixma backend sends
# "<user>  <hostname>" (process user name and gethostname()).
# - user:     add a name for the current UID in front of /etc/passwd, so
#             getpwuid() returns it (the process keeps its UID)
# - hostname: host networking prevents a container hostname, so a small
#             LD_PRELOAD shim returns $SCAN_HOSTNAME from gethostname()
if [ -n "$SCAN_USER" ]; then
  uid=$(id -u); gid=$(id -g)
  if ! head -n1 /etc/passwd | grep -q "^$SCAN_USER:x:$uid:"; then
    sed -i "1i $SCAN_USER:x:$uid:$gid:$SCAN_USER:/nonexistent:/usr/sbin/nologin" /etc/passwd
  fi
fi
if [ -n "$SCAN_HOSTNAME" ]; then
  export LD_PRELOAD="/usr/local/lib/libhostname-shim.so${LD_PRELOAD:+:$LD_PRELOAD}"
fi

# Copy the example config into the volume on first start
mkdir -p /config
[ -f /config/config.yaml ] || cp /app/config.yaml /config/config.yaml

exec python3 -u /app/scan-daemon.py
