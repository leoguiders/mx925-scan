"""Straighten documents that are already in paperless-ngx (3.x).

Workflow: give a document the tag "begradigen" (PAPERLESS_DESKEW_TAG) in the
paperless web UI. This service polls the paperless API, downloads the current
original, straightens every skewed page and uploads the result as a new
*version* of the same document (paperless-ngx >= 3.0, document versions).
ID, title, tags, correspondent, custom fields etc. stay unchanged; the old
version remains available in paperless. Afterwards the tag is removed.

If a document cannot be processed (e.g. a born-digital PDF without scanned
page images), the tag is replaced by PAPERLESS_DESKEW_ERROR_TAG and a note
explains why.
"""
import io
import logging
import os
import threading
import time
from pathlib import Path

import img2pdf
import pikepdf
import requests
from PIL import Image

import deskew

LOG = logging.getLogger("mx925-scan")

API_VERSION = 10


class Skip(Exception):
    """Document cannot be straightened; the message is shown as a note."""


# ------------------------------------------------------------------ Images
def _pdf_pages(data):
    """Yield (PIL image, dpi) for every page of a scanned PDF."""
    pdf = pikepdf.open(io.BytesIO(data))
    for n, page in enumerate(pdf.pages, 1):
        images = list(page.images.values())
        if len(images) != 1:
            raise Skip(f"Seite {n} ist keine reine Scan-Seite "
                       f"({len(images)} Bilder) - nichts geändert.")
        img = pikepdf.PdfImage(images[0]).as_pil_image()
        rot = int(page.obj.get("/Rotate", 0)) % 360
        if rot:
            img = img.rotate(-rot, expand=True)  # /Rotate is clockwise
        box = [float(v) for v in page.mediabox]
        w_pt, h_pt = abs(box[2] - box[0]), abs(box[3] - box[1])
        if rot in (90, 270):
            w_pt, h_pt = h_pt, w_pt
        # The image must cover the page, otherwise it is e.g. a logo
        if abs(img.width / img.height - w_pt / h_pt) > 0.03 * (w_pt / h_pt):
            raise Skip(f"Seite {n} ist keine reine Scan-Seite - nichts geändert.")
        yield img, img.width / (w_pt / 72.0)


def _image_pages(data):
    img = Image.open(io.BytesIO(data))
    dpi = img.info.get("dpi", (300, 300))[0] or 300
    for i in range(getattr(img, "n_frames", 1)):
        img.seek(i)
        yield img.copy(), float(dpi)


def deskew_file(data, mime, name, max_angle=10.0, min_contrast=3.0, quality=90):
    """Returns (bytes, filename, mime, angles) or None if nothing was skewed."""
    if mime == "application/pdf":
        pages = list(_pdf_pages(data))
    elif mime in ("image/jpeg", "image/png", "image/tiff"):
        pages = list(_image_pages(data))
    else:
        raise Skip(f"Dateityp {mime} wird nicht unterstützt - nichts geändert.")

    out, angles = [], []
    for img, dpi in pages:
        if img.mode not in ("1", "L", "RGB"):
            img = img.convert("RGB")
        fixed, angle = deskew.deskew_image(img, max_angle=max_angle,
                                           min_contrast=min_contrast)
        out.append((fixed, dpi))
        angles.append(angle)
    if all(a is None for a in angles):
        return None

    stem = Path(name or "document").stem
    if mime == "application/pdf":
        parts = []
        for img, dpi in out:
            buf = io.BytesIO()
            d = (round(dpi), round(dpi))
            if img.mode == "1":
                img.save(buf, "PNG", dpi=d)
            else:
                img.save(buf, "JPEG", quality=quality, dpi=d)
            parts.append(buf.getvalue())
        return img2pdf.convert(parts), f"{stem}.pdf", mime, angles

    buf = io.BytesIO()
    fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/tiff": "TIFF"}[mime]
    imgs = [i for i, _ in out]
    d = (round(out[0][1]),) * 2
    if fmt == "TIFF":
        imgs[0].save(buf, fmt, save_all=True, append_images=imgs[1:],
                     compression="tiff_lzw", dpi=d)
    elif fmt == "JPEG":
        imgs[0].save(buf, fmt, quality=quality, dpi=d)
    else:
        imgs[0].save(buf, fmt, dpi=d)
    ext = {"JPEG": ".jpg", "PNG": ".png", "TIFF": ".tiff"}[fmt]
    return buf.getvalue(), f"{stem}{ext}", mime, angles


def _label(angles):
    done = [a for a in angles if a is not None]
    if len(angles) == 1:
        return f"Begradigt ({done[0]:+.2f}°)"
    return f"Begradigt ({len(done)}/{len(angles)} Seiten, max {max(map(abs, done)):.1f}°)"


# --------------------------------------------------------------------- API
class Paperless:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Token {token}"
        self.s.headers["Accept"] = f"application/json; version={API_VERSION}"

    def req(self, method, path, **kw):
        r = self.s.request(method, self.url + path, timeout=kw.pop("timeout", 60), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def tag_id(self, name, create=True):
        res = self.req("GET", "/api/tags/", params={"name__iexact": name}).json()
        for t in res.get("results", []):
            if t["name"].lower() == name.lower():
                return t["id"]
        if not create:
            return None
        LOG.info("paperless: creating tag '%s'", name)
        return self.req("POST", "/api/tags/",
                        json={"name": name, "matching_algorithm": 0}).json()["id"]

    def documents_with_tag(self, tag_id):
        res = self.req("GET", "/api/documents/", params={
            "tags__id__all": tag_id, "page_size": 25,
            "fields": "id,title,mime_type,original_file_name"}).json()
        return res.get("results", [])

    def download_original(self, doc_id):
        r = self.req("GET", f"/api/documents/{doc_id}/download/",
                     params={"original": "true"}, timeout=300)
        return r.content, r.headers.get("Content-Type", "").split(";")[0].strip()

    def upload_version(self, doc_id, data, filename, mime, label):
        r = self.req("POST", f"/api/documents/{doc_id}/update_version/",
                     files={"document": (filename, data, mime)},
                     data={"version_label": label[:64]}, timeout=300)
        return r.json()

    def wait_task(self, task_id, timeout=900):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            res = self.req("GET", "/api/tasks/", params={"task_id": task_id}).json()
            items = res.get("results", []) if isinstance(res, dict) else res
            if items:
                t = items[0]
                status = str(t.get("status", "")).lower()
                if status in ("success", "failure", "revoked"):
                    return status, t.get("result_data") or t.get("result")
            time.sleep(5)
        return "timeout", None

    def set_tags(self, doc_id, remove=(), add=()):
        tags = self.req("GET", f"/api/documents/{doc_id}/",
                        params={"fields": "tags"}).json()["tags"]
        new = [t for t in tags if t not in remove] + [t for t in add if t not in tags]
        self.req("PATCH", f"/api/documents/{doc_id}/", json={"tags": new})

    def add_note(self, doc_id, text):
        try:
            self.req("POST", f"/api/documents/{doc_id}/notes/", json={"note": text})
        except Exception as e:
            LOG.warning("paperless: could not add note to #%s: %s", doc_id, e)


# ----------------------------------------------------------------- Service
class DeskewService(threading.Thread):
    def __init__(self, api, tag, error_tag, interval, max_angle, min_contrast, quality):
        super().__init__(daemon=True, name="paperless-deskew")
        self.api, self.tag, self.error_tag = api, tag, error_tag
        self.interval, self.max_angle = interval, max_angle
        self.min_contrast, self.quality = min_contrast, quality

    def run(self):
        LOG.info("paperless: watching for tag '%s' every %.0f s", self.tag, self.interval)
        while True:
            try:
                self.process_once()
            except Exception as e:
                LOG.warning("paperless: %s", e)
            time.sleep(self.interval)

    def process_once(self):
        tag = self.api.tag_id(self.tag)
        err = self.api.tag_id(self.error_tag)
        for doc in self.api.documents_with_tag(tag):
            self.handle(doc, tag, err)

    def handle(self, doc, tag, err):
        did, title = doc["id"], doc.get("title", "")
        LOG.info("paperless: straightening #%s '%s'", did, title)
        try:
            data, mime = self.api.download_original(did)
            mime = mime or doc.get("mime_type", "")
            res = deskew_file(data, mime, doc.get("original_file_name"),
                              self.max_angle, self.min_contrast, self.quality)
        except Skip as e:
            LOG.warning("paperless: #%s skipped: %s", did, e)
            self.api.add_note(did, f"Begradigen: {e}")
            self.api.set_tags(did, remove=[tag], add=[err])
            return
        if res is None:
            LOG.info("paperless: #%s has no detectable skew", did)
            self.api.add_note(did, "Begradigen: kein Schiefstand erkannt - nichts geändert.")
            self.api.set_tags(did, remove=[tag])
            return

        new, filename, new_mime, angles = res
        label = _label(angles)
        LOG.info("paperless: #%s %s - uploading new version", did, label)
        task = self.api.upload_version(did, new, filename, new_mime, label)
        status, result = self.api.wait_task(task)
        if status == "success":
            LOG.info("paperless: #%s new version created", did)
            self.api.set_tags(did, remove=[tag])
        else:
            LOG.error("paperless: #%s version upload %s: %s", did, status, result)
            self.api.add_note(did, f"Begradigen: Hochladen der neuen Version "
                                   f"fehlgeschlagen ({status}): {result}")
            self.api.set_tags(did, remove=[tag], add=[err])


def start_from_env():
    url, token = os.environ.get("PAPERLESS_URL"), os.environ.get("PAPERLESS_TOKEN")
    if not (url and token):
        return None
    svc = DeskewService(
        Paperless(url, token),
        tag=os.environ.get("PAPERLESS_DESKEW_TAG", "begradigen"),
        error_tag=os.environ.get("PAPERLESS_DESKEW_ERROR_TAG", "begradigen-fehler"),
        interval=float(os.environ.get("PAPERLESS_POLL_INTERVAL", "60")),
        max_angle=float(os.environ.get("DESKEW_MAX_ANGLE", "10")),
        min_contrast=float(os.environ.get("DESKEW_MIN_CONTRAST", "3")),
        quality=int(os.environ.get("PAPERLESS_DESKEW_JPEG_QUALITY", "90")),
    )
    svc.start()
    return svc
