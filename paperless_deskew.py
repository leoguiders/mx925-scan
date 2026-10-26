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
import logging
import os
import threading
import time

import requests

from deskew import Skip, _label, deskew_file

LOG = logging.getLogger("mx925-scan")

API_VERSION = 10


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
