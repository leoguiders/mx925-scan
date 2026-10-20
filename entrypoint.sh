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

# Copy the example config into the volume on first start
mkdir -p /config
[ -f /config/config.yaml ] || cp /app/config.yaml /config/config.yaml

exec python3 -u /app/scan-daemon.py
