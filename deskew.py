"""Skew detection and correction for scanned pages.

Method: projection profile. The page is binarised and rotated in small steps;
for each angle the dark pixels per image row are summed. When text lines or
staff lines are exactly horizontal, this row profile has sharp peaks, i.e. a
high variance. The angle with the highest variance is the skew angle.

Works well for sheet music (staff lines) and text documents. Pages without
clear lines (photos, almost empty pages) give a flat score curve; a minimum
"contrast" (best score / median score) prevents rotating those at random.
"""
import io
import logging
from pathlib import Path

import img2pdf
import numpy as np
import pikepdf
from PIL import Image, ImageOps

LOG = logging.getLogger("mx925-scan")


def _score(ink, deg):
    r = ink.rotate(deg, resample=Image.BILINEAR, fillcolor=0)
    prof = np.asarray(r, dtype=np.float32).sum(axis=1)
    return float(np.var(prof))


def estimate_skew(img, max_angle=10.0, work_size=1000, ink_threshold=160):
    """Returns (angle, contrast).

    angle:    degrees counter-clockwise to rotate the page to straighten it
    contrast: best score / median score; ~1 means "no clear lines found"
    """
    g = ImageOps.grayscale(img) if img.mode != "L" else img.copy()
    g.thumbnail((work_size, work_size))
    a = np.asarray(g)
    ink = Image.fromarray(np.where(a < ink_threshold, 255, 0).astype(np.uint8))
    if np.count_nonzero(np.asarray(ink)) < 0.0005 * a.size:
        return 0.0, 1.0  # practically empty page

    # Coarse search over the whole range, then refine around the best angle
    coarse = np.arange(-max_angle, max_angle + 1e-6, 0.5)
    scores = [_score(ink, d) for d in coarse]
    best = float(coarse[int(np.argmax(scores))])
    median = float(np.median(scores)) or 1.0
    best_score = max(scores)
    for step, span in ((0.1, 0.5), (0.02, 0.1)):
        cand = np.arange(best - span, best + span + 1e-6, step)
        cand = cand[np.abs(cand) <= max_angle + 1e-6]
        s = [_score(ink, d) for d in cand]
        i = int(np.argmax(s))
        if s[i] >= best_score:
            best, best_score = float(cand[i]), s[i]
    return best, best_score / median


def deskew_image(img, max_angle=10.0, min_angle=0.1, min_contrast=3.0):
    """Straighten one page. Returns (image, applied_angle or None)."""
    angle, contrast = estimate_skew(img, max_angle)
    if contrast < min_contrast:
        LOG.debug("Deskew: no clear lines (contrast %.2f) - left as is", contrast)
        return img, None
    if abs(angle) < min_angle:
        return img, None
    if abs(angle) >= max_angle - 0.05:
        # Best angle at the edge of the search range: probably not real skew
        LOG.debug("Deskew: angle %.2f at search limit - left as is", angle)
        return img, None
    fill = 255 if img.mode in ("L", "1") else (255, 255, 255)
    src = img.convert("L") if img.mode == "1" else img
    out = src.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)
    # Keep the original page size: crop the expanded canvas back to the centre
    w, h = img.size
    left = (out.width - w) // 2
    top = (out.height - h) // 2
    out = out.crop((left, top, left + w, top + h))
    if img.mode == "1":
        out = out.point(lambda v: 255 if v > 127 else 0).convert("1")
    return out, angle


# --------------------------------------------------------------- Whole files
class Skip(Exception):
    """Document cannot be straightened; the message is shown as a note."""


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
        fixed, angle = deskew_image(img, max_angle=max_angle,
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
        return _reproducible_pdf(parts), f"{stem}.pdf", mime, angles

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


def _reproducible_pdf(images):
    """img2pdf output without date and with a content-derived /ID.

    The same input must always give the same bytes: paperless warns that a
    non-deterministic pre-consume hook can let duplicate documents slip in.
    """
    pdf = pikepdf.open(io.BytesIO(img2pdf.convert(images, nodate=True)))
    if "/ID" in pdf.trailer:
        del pdf.trailer.ID  # random; regenerated from the content below
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True)
    return buf.getvalue()


def _label(angles):
    done = [a for a in angles if a is not None]
    if len(angles) == 1:
        return f"Begradigt ({done[0]:+.2f}°)"
    return f"Begradigt ({len(done)}/{len(angles)} Seiten, max {max(map(abs, done)):.1f}°)"
