FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sane-utils python3 python3-sane python3-yaml python3-pil python3-img2pdf \
      tini tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scan-daemon.py config.yaml entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh /app/scan-daemon.py

# Unhealthy if the polling loop has not touched the heartbeat for 2 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 120 || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
