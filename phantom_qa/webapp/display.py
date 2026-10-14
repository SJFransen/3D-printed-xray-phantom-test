"""Shared automatic window for the image renderer and W/C controls."""

import math

import numpy as np


def display_range(pixels):
    """Use the same 1--99 percentile stretch in the viewer and its controls."""
    minimum, maximum = float(np.min(pixels)), float(np.max(pixels))
    lo, hi = (float(v) for v in np.percentile(pixels, [1, 99]))
    if hi - lo < 1e-6:
        lo, hi = minimum, maximum
    if hi - lo < 1e-6:
        lo, hi = lo - 0.5, hi + 0.5
    return {"lo": lo, "hi": hi, "min": minimum, "max": maximum}


def window_image(pixels, wc=None, ww=None):
    """Map source values to an 8-bit preview without changing analysis pixels."""
    if (wc is None or ww is None or not math.isfinite(wc)
            or not math.isfinite(ww) or ww <= 0):
        limits = display_range(pixels)
        lo, hi = limits["lo"], limits["hi"]
    else:
        lo, hi = wc - ww / 2, wc + ww / 2
    a = np.clip((np.asarray(pixels, dtype=float) - lo) / max(hi - lo, 1e-9), 0, 1)
    return (a * 255).astype(np.uint8)
