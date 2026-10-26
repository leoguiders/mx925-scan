"""Skew detection and correction for scanned pages.

Method: projection profile. The page is binarised and rotated in small steps;
for each angle the dark pixels per image row are summed. When text lines or
staff lines are exactly horizontal, this row profile has sharp peaks, i.e. a
high variance. The angle with the highest variance is the skew angle.

Works well for sheet music (staff lines) and text documents. Pages without
clear lines (photos, almost empty pages) give a flat score curve; a minimum
"contrast" (best score / median score) prevents rotating those at random.
"""
import logging

import numpy as np
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
