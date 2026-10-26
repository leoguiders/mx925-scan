# --- Build the gethostname() shim (see hostname-shim.c) ---------------------
FROM debian:trixie-slim AS shim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY hostname-shim.c /src/
RUN gcc -O2 -Wall -Werror -shared -fPIC -o /src/libhostname-shim.so /src/hostname-shim.c -ldl

# --- Build SANE (pixma backend only) from source -----------------------------
# Debian ships an older SANE; the current upstream master has many BJNP fixes
# that are not in any release yet. The patches in patches/ are applied on top
# (PIXMA_POLL_DELAY_MS). Override SANE_REF to pin a branch, tag or commit.
FROM debian:trixie-slim AS sane
ARG SANE_REPO=https://gitlab.com/sane-project/backends.git
ARG SANE_REF=master
ARG SANE_VERSION=1.4.0-git
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential autoconf automake autoconf-archive \
      autopoint libtool gettext pkg-config python3 \
      libusb-1.0-0-dev libjpeg-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "$SANE_REF" "$SANE_REPO" /src/sane \
 || (git clone "$SANE_REPO" /src/sane && git -C /src/sane checkout "$SANE_REF")
WORKDIR /src/sane
COPY patches/ /src/patches/
RUN for p in /src/patches/*.patch; do \
      [ -e "$p" ] || continue; \
      echo "Applying $p"; \
      git apply --verbose "$p" || { echo "Patch $p does not apply to $SANE_REF" >&2; exit 1; }; \
    done
RUN echo "$SANE_VERSION" > .tarball-version \
 && git log -1 --format='%H %s' > /src/sane-commit.txt \
 && ./autogen.sh \
 && ./configure --prefix=/usr/local --sysconfdir=/etc --localstatedir=/var \
      BACKENDS="pixma" --disable-locking \
      --without-snmp --without-avahi --without-libcurl --without-poppler-glib \
      --without-usb-record-replay \
 && make -j"$(nproc)" \
 && make install DESTDIR=/out \
 && rm -rf /out/usr/local/share/doc /out/usr/local/share/man /out/usr/local/include \
 && cp /src/sane-commit.txt /out/usr/local/share/sane-commit.txt

# --- Runtime image -----------------------------------------------------------
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# python3-sane pulls in Debian's libsane1; the self-built libsane in
# /usr/local/lib takes precedence (ldconfig search order), and scanimage comes
# from /usr/local/bin (no sane-utils package).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-sane python3-yaml python3-pil python3-img2pdf \
      libusb-1.0-0 libjpeg62-turbo tini tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=sane /out/usr/local /usr/local
COPY --from=shim /src/libhostname-shim.so /usr/local/lib/libhostname-shim.so

# Remove Debian's libsane and backends, so only the self-built SANE can load
RUN rm -rf /usr/lib/*-linux-gnu/libsane.so* /usr/lib/*-linux-gnu/sane \
 && ldconfig

# Sanity checks, one per step so a failure shows up clearly in the build log
RUN ldd /usr/local/bin/scanimage /usr/local/lib/sane/libsane-pixma.so.1 | grep "not found" \
      && { echo "missing libraries (see above)" >&2; exit 1; } || true
RUN scanimage -V
RUN python3 -c "import sane; print('python-sane', sane.init())"

WORKDIR /app
COPY scan-daemon.py config.yaml entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh /app/scan-daemon.py

# Unhealthy if the polling loop has not touched the heartbeat for 2 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 120 || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
