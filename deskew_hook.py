#!/usr/bin/env python3
"""paperless-ngx pre-consume hook: straighten skewed scanned pages.

Runs inside the paperless container for every document before it is
processed, regardless of its source (consume folder, upload, e-mail, new
versions). Uses the same detection as mx925-scan (deskew.py, which must lie
next to this file) and replaces paperless' own deskewing, which works poorly
on sheet music. Set PAPERLESS_OCR_DESKEW=false in paperless when using it.

Configuration (environment of the paperless container, all optional):
  DESKEW_MAX_ANGLE     search range in degrees (default 10)
  DESKEW_MIN_CONTRAST  confidence threshold (default 3)

The hook never blocks consumption: on any problem the file stays unchanged.
Output goes to the paperless log.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".tif": "image/tiff", ".tiff": "image/tiff"}


def main():
    path = os.environ.get("DOCUMENT_WORKING_PATH", "")
    mime = MIME.get(os.path.splitext(path)[1].lower())
    if not path or not os.path.isfile(path) or not mime:
        return
    try:
        import deskew
    except Exception as e:  # missing module or library
        print(f"deskew-hook: not available ({e}) - document left unchanged")
        return

    name = os.path.basename(os.environ.get("DOCUMENT_SOURCE_PATH", path))
    try:
        with open(path, "rb") as f:
            data = f.read()
        res = deskew.deskew_file(
            data, mime, name,
            max_angle=float(os.environ.get("DESKEW_MAX_ANGLE", "10")),
            min_contrast=float(os.environ.get("DESKEW_MIN_CONTRAST", "3")))
    except deskew.Skip as e:
        print(f"deskew-hook: {name}: {e}")
        return
    except Exception as e:
        print(f"deskew-hook: {name}: error ({e}) - document left unchanged")
        return
    if res is None:
        print(f"deskew-hook: {name}: no skew detected")
        return

    new, _, _, angles = res
    tmp = path + ".deskew-tmp"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, path)
    pages = ", ".join(f"p{n} {a:+.2f}°" for n, a in enumerate(angles, 1) if a is not None)
    print(f"deskew-hook: {name}: straightened {pages}")


if __name__ == "__main__":
    main()
    sys.exit(0)
