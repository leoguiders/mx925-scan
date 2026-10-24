# --- Build the gethostname() shim (see hostname-shim.c) ---------------------
FROM debian:trixie-slim AS shim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY hostname-shim.c /src/
RUN gcc -O2 -Wall -Werror -shared -fPIC -o /src/libhostname-shim.so /src/hostname-shim.c -ldl

# --- Build SANE (pixma backend only) from source -----------------------------
# Debian ships an older SANE; current upstream has many BJNP fixes, and the
# fork adds PIXMA_POLL_DELAY_MS. Override SANE_REPO / SANE_REF to use another
# repository, branch, tag or commit.
FROM debian:trixie-slim AS sane
ARG SANE_REPO=https://github.com/leoguiders/sane-backends.git
ARG SANE_REF=mx925
ARG SANE_VERSION=1.4.0-mx925
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential autoconf automake autoconf-archive \
      autopoint libtool gettext pkg-config python3 \
      libusb-1.0-0-dev libjpeg-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "$SANE_REF" "$SANE_REPO" /src/sane \
 || (git clone "$SANE_REPO" /src/sane && git -C /src/sane checkout "$SANE_REF")
WORKDIR /src/sane
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
RUN ldconfig \
 && scanimage -V \
 && ldd "$(python3 -c 'import _sane; print(_sane.__file__)')" | grep -q '/usr/local/lib/libsane.so.1' \
 || (echo "python3-sane does not use the self-built libsane" >&2; exit 1)

WORKDIR /app
COPY scan-daemon.py config.yaml entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh /app/scan-daemon.py

# Unhealthy if the polling loop has not touched the heartbeat for 2 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 120 || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
