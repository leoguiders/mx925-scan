#!/usr/bin/env python3
"""MX925 scan daemon

Keeps a SANE connection (pixma backend / BJNP) to a Canon PIXMA open and waits
for a scan to be started on the printer's operation panel (colour/black start
button). The settings the printer sends along with that event (resolution,
source, duplex, format, scan size, image corrections ...) are mapped to scan
settings via config.yaml. The result is written to an output folder, e.g. the
paperless-ngx consume directory.
"""
import datetime
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import img2pdf
import sane
import yaml
from PIL import Image, ImageFilter, ImageOps

LOG = logging.getLogger("mx925-scan")

CONFIG_PATH = os.environ.get("CONFIG", "/config/config.yaml")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1"))
COOLDOWN = float(os.environ.get("COOLDOWN", "5"))
# Pause between closing the polling connection and starting scanimage
SETTLE_DELAY = float(os.environ.get("SETTLE_DELAY", "2"))
# On BJNP connection errors: delay and number of retries
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "10"))
RETRIES = int(os.environ.get("RETRIES", "2"))
IO_ERRORS = ("Error during device I/O", "Can not connect", "sane_read: Invalid argument",
             "sane_start: Invalid argument")
# Reopen the idle polling connection periodically (seconds, 0 = never) in
# case the printer silently drops the session, e.g. after power saving
RECONNECT_INTERVAL = float(os.environ.get("RECONNECT_INTERVAL", "1800"))
PUID = os.environ.get("PUID")
PGID = os.environ.get("PGID")
HEARTBEAT = Path("/tmp/heartbeat")

# Read-only options of the pixma backend that the printer fills on scan start
SENSORS = ("button-1", "button-2", "target", "original", "scan-resolution",
           "document-type", "adf-status", "adf-orientation")

RUNNING = True


def _stop(*_):
    global RUNNING
    RUNNING = False


# ------------------------------------------------------------------ Config
def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return {
        "defaults": cfg.get("defaults") or {},
        "buttons": cfg.get("buttons") or {},
        "rules": cfg.get("rules") or [],
    }


def resolve_settings(cfg, sensors):
    s = {
        "mode": "Color", "resolution": 300, "source": "auto", "duplex": False,
        "jpeg_quality": 85, "duplex_rotate_back": 0, "skip_blank": False,
        "blank_threshold": 0.003, "filename": "scan_%Y-%m-%d_%H-%M-%S",
        "subdir": "", "extra_args": [],
    }
    s.update(cfg["defaults"])

    for button, overrides in cfg["buttons"].items():
        if sensors.get(button):
            s.update(overrides or {})

    for rule in cfg["rules"]:
        cond = rule.get("when") or {}
        if cond and all(lookup(sensors, k) == v for k, v in cond.items()):
            s.update(rule.get("set") or {})
    return s


def lookup(sensors, key):
    """Sensor value or a byte of the raw event packet (e.g. 'byte-09')."""
    if str(key).startswith("byte-"):
        pkt = sensors.get("_intr")
        try:
            idx = int(str(key)[5:], 16)
        except ValueError:
            return None
        return pkt[idx] if pkt and idx < len(pkt) else None
    return sensors.get(key)


# --------------------------------------------------- Raw event packet (INTR)
class IntrCapture:
    """Captures the event packet the printer sends when a scan is started.

    The pixma backend only decodes part of it (e.g. not duplex). With
    SANE_DEBUG_PIXMA=11 it writes the packet as a hex dump to stderr, so
    stderr is redirected into a pipe and parsed here.
    """
    START = re.compile(r"\[pixma\]\s+INTR\b.*len=(\d+)")
    HEX = re.compile(r"\[pixma\]\s+([0-9a-fA-F]{8}):(.*)$")
    BYTE = re.compile(r"[0-9a-fA-F]{2}")

    def __init__(self):
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.packet = None
        self._buf = None
        self._len = 0
        self._got = 0

    def install(self):
        orig = os.dup(2)
        sys.stderr = os.fdopen(orig, "w", buffering=1)
        r, w = os.pipe()
        os.dup2(w, 2)
        os.close(w)
        threading.Thread(target=self._reader, args=(r,), daemon=True).start()

    def _reader(self, fd):
        with os.fdopen(fd, "r", errors="replace") as f:
            for line in f:
                try:
                    self._handle(line.rstrip("\n"))
                except Exception:
                    pass

    def _handle(self, line):
        m = self.START.search(line)
        if m:
            self._len = int(m.group(1))
            self._buf = bytearray(self._len)
            self._got = 0
            return
        if self._buf is not None:
            m = self.HEX.search(line)
            if m:
                off = int(m.group(1), 16)
                want = min(16, self._len - off)
                vals = []
                for tok in m.group(2).split():
                    if len(vals) >= want or not self.BYTE.fullmatch(tok):
                        break
                    vals.append(int(tok, 16))
                self._buf[off:off + len(vals)] = bytes(vals)
                self._got = max(self._got, off + len(vals))
                if self._got >= self._len:
                    self._finish()
                return
            self._finish()
        # Drop debug noise from the C library, pass errors through
        if any(t in line for t in ("[pixma]", "[sanei_debug]", "[bjnp]")) \
                and "ERROR" not in line:
            return
        if line.strip():
            sys.stderr.write(line + "\n")

    def _finish(self):
        with self.lock:
            self.packet = bytes(self._buf)
            self._buf = None
        self.event.set()

    def get(self, timeout=1.0):
        self.event.wait(timeout)
        with self.lock:
            return self.packet

    def clear(self):
        with self.lock:
            self.packet = None
        self.event.clear()


# -------------------------------------------------------------------- SANE
def open_device(name):
    if not name:
        found = [d[0] for d in sane.get_devices() if d[0].startswith("pixma:")]
        if not found:
            raise RuntimeError("No pixma scanner found (check SCANNER_IP)")
        name = found[0]
    LOG.info("Connecting to %s", name)
    return name, sane.open(name)


def read_sensors(dev):
    values = {}
    for opt in SENSORS:
        try:
            values[opt] = getattr(dev, opt.replace("-", "_"))
        except Exception:
            pass  # option inactive or not supported by this model
    if not values:
        # Nothing readable at all -> connection lost, trigger a reconnect
        raise RuntimeError("Scanner does not respond to polling")
    return values


def source_names(dev):
    try:
        available = list(dev.opt["source"].constraint or [])
    except Exception:
        available = []

    def find(pred):
        return next((s for s in available if pred(s.lower())), None)

    return {
        "flatbed": find(lambda s: "flatbed" in s),
        "adf": find(lambda s: ("adf" in s or "feeder" in s) and "duplex" not in s),
        "duplex": find(lambda s: "duplex" in s),
    }, available


# -------------------------------------------------------------------- Scan
class ScannerStuck(Exception):
    """Printer reports 'PCScanning' - stuck in an unfinished scan job."""


def run_scan(device, source, s, workdir):
    """Returns (pages, io_error, feeder_empty)."""
    Path(workdir).mkdir(parents=True, exist_ok=True)
    cmd = ["scanimage", "-d", device, "--source", source,
           "--mode", str(s["mode"]), "--resolution", str(s["resolution"]),
           "--format=pnm", f"--batch={workdir}/p%04d.pnm"]
    if "flatbed" in source.lower():
        cmd.append("--batch-count=1")
    # Scan area in mm (empty = maximum area)
    if s.get("page_width"):
        cmd += ["-x", str(s["page_width"])]
    if s.get("page_height"):
        cmd += ["-y", str(s["page_height"])]
    cmd += [str(a) for a in (s.get("extra_args") or [])]
    LOG.info("Running: %s", " ".join(cmd))
    # Debug output only in the daemon, not in scanimage (would be megabytes)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANE_DEBUG")}
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    err = res.stderr or ""
    if err.strip():
        LOG.debug("scanimage: %s", err.strip())
    pages = sorted(Path(workdir).glob("p*.pnm"))
    # Discard empty / aborted files
    pages = [p for p in pages if p.stat().st_size > 1024]
    if "Previous scan attempt was not completed" in err:
        raise ScannerStuck()
    io_error = any(m in err for m in IO_ERRORS)
    if io_error and pages:
        # The page during which the connection broke may be incomplete
        LOG.warning("Connection error after page %d - discarding that page", len(pages))
        pages = pages[:-1]
    empty = "out of documents" in err.lower() and not pages
    return pages, io_error, empty


def is_blank(img, threshold):
    g = ImageOps.grayscale(img)
    g.thumbnail((400, 400))
    w, h = g.size
    # Ignore the border (shadows, feeder edges)
    g = g.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95)))
    dark = sum(g.histogram()[:200])
    return dark / max(1, g.size[0] * g.size[1]) < threshold


def auto_crop(img, dpi, threshold=225, margin_mm=2.0):
    """Crop to the content (for 'auto scan' size on the flatbed).

    Finds the area darker than the white lid. Dust specks are removed first
    with a median filter on a downscaled copy.
    """
    g = ImageOps.grayscale(img)
    scale = max(1, g.width // 600)
    small = g.resize((g.width // scale, g.height // scale)).filter(ImageFilter.MedianFilter(5))
    mask = small.point(lambda v: 255 if v < threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return img
    pad = int(margin_mm / 25.4 * dpi)
    x0, y0, x1, y1 = (v * scale for v in bbox)
    box = (max(0, x0 - pad), max(0, y0 - pad),
           min(img.width, x1 + pad), min(img.height, y1 + pad))
    # Only crop if it is worth it (otherwise keep the full page)
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.95 * img.width * img.height:
        return img
    LOG.info("Auto-cropped to %dx%d mm",
             round((box[2] - box[0]) / dpi * 25.4), round((box[3] - box[1]) / dpi * 25.4))
    return img.crop(box)


def whiten_background(img, margin=12.0):
    """Make the paper background pure white.

    The background level is measured per page: on documents most of the area
    is paper, so the median brightness is a good estimate. Everything at or
    above (background - margin) becomes white, darker tones are stretched
    linearly. Grey or yellowed paper ends up white, text stays dark.
    Pages that are mostly dark (photos) are left untouched.
    """
    small = img.copy()
    small.thumbnail((600, 600))

    def median(hist):
        total, acc = sum(hist), 0
        for v, n in enumerate(hist):
            acc += n
            if acc >= total / 2:
                return v
        return 255

    if median(ImageOps.grayscale(small).histogram()) < 128:
        LOG.debug("Background too dark - not whitened")
        return img
    # One curve per colour channel, so yellowed paper becomes neutral white
    lut = []
    for band in small.split():
        t = max(128, min(254, int(median(band.histogram()) - margin)))
        lut += [min(255, v * 255 // t) for v in range(256)]
    return img.point(lut)


def prepare_pages(pages, s, duplex):
    """Load pages, rotate, drop blank pages and apply image corrections."""
    rotate = int(s.get("duplex_rotate_back") or 0) if duplex else 0
    images = []
    for i, page in enumerate(pages):
        img = Image.open(page)
        if rotate and i % 2 == 1:
            img = img.rotate(rotate, expand=True)
        page_rotate = int(s.get("page_rotate") or 0)  # counter-clockwise
        if page_rotate:
            img = img.rotate(page_rotate, expand=True)
        if s.get("skip_blank") and is_blank(img, float(s["blank_threshold"])):
            LOG.info("Page %d is blank - skipped", i + 1)
            continue
        if img.mode not in ("1", "L", "RGB"):
            img = img.convert("RGB")
        if s.get("auto_crop"):
            img = auto_crop(img, int(s["resolution"]),
                            int(s.get("auto_crop_threshold", 225)),
                            float(s.get("auto_crop_margin", 2)))
        if s.get("descreen") and img.mode != "1":
            # Slightly blur halftone screens (magazines, brochures); the radius
            # scales with the resolution, the unsharp mask sharpens afterwards
            r = float(s.get("descreen_radius", 1.0)) * int(s["resolution"]) / 300
            if r >= 0.3:
                img = img.filter(ImageFilter.GaussianBlur(r))
        if s.get("whiten_background") and img.mode != "1":
            img = whiten_background(img, float(s.get("background_margin", 12)))
        if s.get("reduce_bleed") and img.mode != "1":
            # Levels: everything lighter than 'bleed_threshold' becomes white,
            # the rest is stretched linearly (text stays dark)
            t = max(1, min(254, int(s.get("bleed_threshold", 210))))
            lut = [min(255, v * 255 // t) for v in range(256)]
            img = img.point(lut * len(img.getbands()))
        if s.get("unsharp_mask") and img.mode != "1":
            img = img.filter(ImageFilter.UnsharpMask(
                radius=float(s.get("unsharp_radius", 2)),
                percent=int(s.get("unsharp_percent", 80)),
                threshold=int(s.get("unsharp_threshold", 3))))
        images.append(img)
    return images


def build_output(images, s, workdir):
    """Creates the output files in the requested format.

    format: pdf  - one PDF (JPEG pages, lineart lossless)
            tiff - one multi-page TIFF (LZW, lineart G4)
            jpeg - one JPEG per page (lineart as PNG)
            png  - one PNG per page
    """
    fmt = str(s.get("format", "pdf")).lower()
    fmt = {"jpg": "jpeg", "tif": "tiff"}.get(fmt, fmt)
    dpi = int(s["resolution"])
    q = int(s["jpeg_quality"])
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if fmt == "tiff":
        out = workdir / "out.tiff"
        comp = "group4" if all(i.mode == "1" for i in images) else "tiff_lzw"
        images[0].save(out, save_all=True, append_images=images[1:],
                       compression=comp, dpi=(dpi, dpi))
        return [out]

    singles = []
    for n, img in enumerate(images, 1):
        if img.mode == "1" or fmt == "png":
            f = workdir / f"p{n:04d}.png"
            img.save(f, dpi=(dpi, dpi))
        else:
            f = workdir / f"p{n:04d}.jpg"
            img.save(f, quality=q, dpi=(dpi, dpi))
        singles.append(f)

    if fmt in ("jpeg", "png"):
        return singles

    if fmt != "pdf":
        LOG.warning("Unknown format '%s' - creating PDF", fmt)
    out = workdir / "out.pdf"
    with open(out, "wb") as fh:
        fh.write(img2pdf.convert([str(f) for f in singles]))
    return [out]


def _chown(path):
    if PUID and PGID:
        try:
            os.chown(path, int(PUID), int(PGID))
        except OSError as e:
            LOG.warning("chown %s: %s", path, e)


def deliver(files, s):
    out_dir = OUTPUT_DIR / str(s.get("subdir") or "")
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        _chown(out_dir)
    base = datetime.datetime.now().strftime(str(s["filename"]))
    multi = len(files) > 1
    for i, src in enumerate(files, 1):
        stem = f"{base}_{i:03d}" if multi else base
        ext = Path(src).suffix
        final = out_dir / f"{stem}{ext}"
        n = 1
        while final.exists():
            final = out_dir / f"{stem}_{n}{ext}"
            n += 1
        # Write as .part first, then rename atomically, so paperless never
        # picks up a half-written file
        part = final.with_name(final.name + ".part")
        shutil.copyfile(src, part)
        _chown(part)
        os.chmod(part, 0o664)
        os.replace(part, final)
        LOG.info("Saved: %s", final)


def handle_trigger(device, names, cfg, sensors):
    s = resolve_settings(cfg, sensors)
    LOG.info("Settings: mode=%s dpi=%s source=%s duplex=%s format=%s",
             s["mode"], s["resolution"], s["source"], s["duplex"],
             s.get("format", "pdf"))

    feeder = "duplex" if s.get("duplex") else "adf"
    want = str(s.get("source", "auto")).lower()
    if want == "auto":
        adf = sensors.get("adf-status")
        if adf == 2:
            candidates = [feeder]
        elif adf == 1:
            candidates = ["flatbed"]
        else:  # printer reports nothing -> try ADF first, then flatbed
            candidates = [feeder, "flatbed"]
    elif want == "adf":
        candidates = [feeder]
    else:
        candidates = [want]

    # Give the scanner time to release the polling session
    time.sleep(SETTLE_DELAY)

    try:
        _scan_candidates(device, names, s, candidates)
    except ScannerStuck:
        LOG.error("Printer is stuck in an unfinished scan (PCScanning). "
                  "Press 'Stop' on the printer or power-cycle it.")


def _scan_candidates(device, names, s, candidates):
    with tempfile.TemporaryDirectory() as tmp:
        for cand in candidates:
            source = names.get(cand) if cand in names else s["source"]
            if source is None and cand == "duplex":
                LOG.warning("No duplex feeder found - scanning simplex")
                source = names.get("adf")
            if not source:
                continue

            for attempt in range(RETRIES + 1):
                workdir = Path(tmp) / f"{cand}-{attempt}"
                pages, io_error, empty = run_scan(device, source, s, workdir)
                if pages:
                    LOG.info("%d page(s) from '%s'", len(pages), source)
                    images = prepare_pages(pages, s, "duplex" in source.lower())
                    if images:
                        deliver(build_output(images, s, Path(tmp) / "out"), s)
                    else:
                        LOG.warning("All pages blank - nothing saved")
                    if io_error:
                        LOG.warning("Scan was aborted by a connection error - "
                                    "please rescan the remaining pages")
                    return
                if not io_error:
                    break
                if attempt < RETRIES:
                    LOG.warning("Scanner not responding (BJNP) - retry %d/%d in %.0f s",
                                attempt + 1, RETRIES, RETRY_DELAY)
                    time.sleep(RETRY_DELAY)

            if io_error:
                # Do NOT fall back to the flatbed on connection errors
                LOG.error("Scanner unreachable - scan aborted")
                return
            if empty:
                LOG.info("Feeder '%s' is empty", source)
        LOG.warning("Scan returned no pages")


# -------------------------------------------------------------------- Main
def main():
    capture = None
    if os.environ.get("RAW_EVENTS", "1") == "1":
        os.environ["SANE_DEBUG_PIXMA"] = "11"
        capture = IntrCapture()
        capture.install()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sane.init()
    device = os.environ.get("SCANNER_DEVICE", "").strip()
    dev, names, last, opened_at = None, {}, None, 0.0

    while RUNNING:
        try:
            if dev is not None and RECONNECT_INTERVAL > 0 \
                    and time.monotonic() - opened_at > RECONNECT_INTERVAL:
                LOG.debug("Reopening idle connection")
                dev.close()
                dev = None
            if dev is None:
                device, dev = open_device(device)
                opened_at = time.monotonic()
                names, available = source_names(dev)
                LOG.info("Available sources: %s", available)
                LOG.info("Ready - waiting for a scan to be started on the printer")

            sensors = read_sensors(dev)
            HEARTBEAT.touch()
            if sensors != last:
                LOG.debug("Sensors: %s", sensors)
                last = sensors

            if sensors.get("button-1") or sensors.get("button-2"):
                LOG.info("Scan started on printer - values: %s", sensors)
                if capture:
                    pkt = capture.get(1.0)
                    if pkt:
                        sensors["_intr"] = pkt
                        LOG.info("Event packet: %s", " ".join(
                            f"{b:02x}" for b in pkt[:32]))
                    else:
                        LOG.warning("No event packet captured")
                cfg = load_config()  # pick up changes without a restart
                dev.close()          # scanimage needs exclusive access
                dev = None
                handle_trigger(device, names, cfg, sensors)
                if capture:
                    capture.clear()
                time.sleep(COOLDOWN)
                continue
        except Exception as e:
            LOG.warning("Error: %s - retrying in 10 s", e)
            try:
                if dev:
                    dev.close()
            except Exception:
                pass
            dev = None
            time.sleep(10)
            continue
        time.sleep(POLL_INTERVAL)

    if dev:
        dev.close()
    sane.exit()
    LOG.info("Stopped")


if __name__ == "__main__":
    main()
