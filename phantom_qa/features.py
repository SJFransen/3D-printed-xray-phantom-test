"""Sub-pixel measurement primitives: profile sampling, line/edge localization, ROI stats.

Coordinates are (x, y) = (column, row) in pixels unless suffixed ``_mm``.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage


# ---------------------------------------------------------------- profile sampling

def sample_profile(img: np.ndarray, p0, p1, n: int, avg_half_width: float = 0.0,
                   n_avg: int = 1):
    """Sample ``n`` points along segment p0->p1 (bilinear). If ``avg_half_width`` > 0,
    average ``n_avg`` parallel profiles offset perpendicular within +/- half width.
    Returns (positions_px_along, values)."""
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    d = p1 - p0
    length = float(np.hypot(*d))
    u = d / max(length, 1e-9)
    perp = np.array([-u[1], u[0]])
    ts = np.linspace(0.0, length, n)
    base = p0[None, :] + ts[:, None] * u[None, :]

    if avg_half_width > 0 and n_avg > 1:
        offsets = np.linspace(-avg_half_width, avg_half_width, n_avg)
    else:
        offsets = np.array([0.0])

    acc = np.zeros(n)
    for off in offsets:
        pts = base + off * perp[None, :]
        acc += ndimage.map_coordinates(img, [pts[:, 1], pts[:, 0]], order=1,
                                       mode="nearest")
    return ts, acc / len(offsets)


# ---------------------------------------------------------------- 1-D localization

def parabolic_refine(y: np.ndarray, i: int) -> float:
    """Refine an extremum index by parabola through (i-1, i, i+1). Returns float index."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    denom = y[i - 1] - 2 * y[i] + y[i + 1]
    if abs(denom) < 1e-12:
        return float(i)
    return i + 0.5 * (y[i - 1] - y[i + 1]) / denom


def centroid_peak(y: np.ndarray, i: int, half: int = 4) -> float:
    """Sub-sample peak position via intensity centroid above local baseline."""
    lo, hi = max(0, i - half), min(len(y), i + half + 1)
    seg = y[lo:hi].astype(float)
    base = seg.min()
    w = seg - base
    if w.sum() <= 0:
        return float(i)
    return lo + float(np.sum(np.arange(len(seg)) * w) / w.sum())


def find_peaks_1d(y: np.ndarray, min_distance: int = 3, threshold_rel: float = 0.3):
    """Simple robust peak finder: local maxima above rel threshold of (max-min)."""
    y = np.asarray(y, float)
    thr = y.min() + threshold_rel * (y.max() - y.min() + 1e-12)
    idx = []
    for i in range(1, len(y) - 1):
        if y[i] >= y[i - 1] and y[i] > y[i + 1] and y[i] > thr:
            if idx and i - idx[-1] < min_distance:
                if y[i] > y[idx[-1]]:
                    idx[-1] = i
            else:
                idx.append(i)
    return np.array(idx, dtype=int)


def edge_position(y: np.ndarray, rising: bool | None = None,
                  smooth_sigma: float = 2.0) -> float:
    """Sub-pixel edge location = extremum of the smoothed derivative (parabola-refined).

    ``rising=None`` picks the strongest edge of either sign."""
    ys = ndimage.gaussian_filter1d(np.asarray(y, float), smooth_sigma)
    dy = np.gradient(ys)
    if rising is True:
        i = int(np.argmax(dy))
        return parabolic_refine(dy, i)
    if rising is False:
        i = int(np.argmin(dy))
        return parabolic_refine(-dy, i)
    i = int(np.argmax(np.abs(dy)))
    sign = 1.0 if dy[i] >= 0 else -1.0
    return parabolic_refine(sign * dy, i)


# ---------------------------------------------------------------- line localization

def locate_line_center(img: np.ndarray, near_pt, direction, search_half: float,
                       n: int = 61, avg_half_width: float = 3.0, n_avg: int = 5,
                       bright: bool = True):
    """Locate the center of a (bright) line crossing point ``near_pt`` roughly
    perpendicular to ``direction``. Samples a profile along ``direction`` through
    the point and finds the peak with sub-pixel refinement.

    Returns (point_xy, strength) where strength = peak prominence in image units,
    or (None, 0.0) if no clear line found."""
    near_pt = np.asarray(near_pt, float)
    u = np.asarray(direction, float)
    u = u / max(np.hypot(*u), 1e-9)
    p0 = near_pt - search_half * u
    p1 = near_pt + search_half * u
    ts, vals = sample_profile(img, p0, p1, n, avg_half_width, n_avg)
    v = vals if bright else -vals
    i = int(np.argmax(v))
    prominence = float(v[i] - np.median(v))
    noise = float(np.std(np.diff(v))) + 1e-9
    if prominence < 4 * noise:
        return None, 0.0
    ti = centroid_peak(v, i, half=3)
    t = np.interp(ti, np.arange(len(ts)), ts)
    return tuple(p0 + t * u), prominence


# ---------------------------------------------------------------- ROI statistics

def rect_mask(shape, center, size, angle_deg: float = 0.0):
    """Boolean mask of a (possibly rotated) rectangle. center=(x,y), size=(w,h) px."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    dx = xx - center[0]
    dy = yy - center[1]
    a = np.deg2rad(angle_deg)
    lx = dx * np.cos(a) + dy * np.sin(a)
    ly = -dx * np.sin(a) + dy * np.cos(a)
    # Half-open bounds keep an integral-width rectangle at a stable sample
    # count when translated across the pixel grid. The tolerance absorbs the
    # determinant/rotation roundoff (e.g. a 70 px side becoming 69.99999999999999).
    eps = 1e-9
    return ((lx >= -size[0] / 2 - eps) & (lx < size[0] / 2 - eps)
            & (ly >= -size[1] / 2 - eps) & (ly < size[1] / 2 - eps))


def circle_mask(shape, center, radius):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx - center[0]) ** 2 + (yy - center[1]) ** 2 <= radius ** 2


def roi_stats_masked(img: np.ndarray, mask: np.ndarray) -> dict:
    vals = img[mask]
    return {
        "mean": float(vals.mean()) if vals.size else float("nan"),
        "std": float(vals.std(ddof=1)) if vals.size > 1 else float("nan"),
        "min": float(vals.min()) if vals.size else float("nan"),
        "max": float(vals.max()) if vals.size else float("nan"),
        "n": int(vals.size),
    }


def roi_stats_rect(img, center, size, angle_deg=0.0) -> dict:
    """Stats over a rotated rect using a local crop for efficiency."""
    cx, cy = center
    r = 0.5 * float(np.hypot(*size)) + 2
    x0, x1 = int(max(0, cx - r)), int(min(img.shape[1], cx + r + 1))
    y0, y1 = int(max(0, cy - r)), int(min(img.shape[0], cy + r + 1))
    sub = img[y0:y1, x0:x1]
    m = rect_mask(sub.shape, (cx - x0, cy - y0), size, angle_deg)
    return roi_stats_masked(sub, m)


def annulus_mask(shape, center, r_inner, r_outer):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    d2 = (xx - center[0]) ** 2 + (yy - center[1]) ** 2
    return (d2 <= r_outer ** 2) & (d2 >= r_inner ** 2)


def roi_stats_annulus(img, center, r_inner, r_outer) -> dict:
    cx, cy = center
    r = r_outer + 2
    x0, x1 = int(max(0, cx - r)), int(min(img.shape[1], cx + r + 1))
    y0, y1 = int(max(0, cy - r)), int(min(img.shape[0], cy + r + 1))
    sub = img[y0:y1, x0:x1]
    m = annulus_mask(sub.shape, (cx - x0, cy - y0), r_inner, r_outer)
    return roi_stats_masked(sub, m)


def roi_stats_circle(img, center, radius) -> dict:
    cx, cy = center
    r = radius + 2
    x0, x1 = int(max(0, cx - r)), int(min(img.shape[1], cx + r + 1))
    y0, y1 = int(max(0, cy - r)), int(min(img.shape[0], cy + r + 1))
    sub = img[y0:y1, x0:x1]
    m = circle_mask(sub.shape, (cx - x0, cy - y0), radius)
    return roi_stats_masked(sub, m)


def fit_line_tls(points: np.ndarray):
    """Total-least-squares line fit. Returns (point_on_line, unit_direction)."""
    pts = np.asarray(points, float)
    c = pts.mean(axis=0)
    u, s, vt = np.linalg.svd(pts - c)
    return c, vt[0]


def linear_fit_r2(x, y):
    """Least-squares y = a*x + b. Returns dict(a, b, r2, residuals)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    A = np.vstack([x, np.ones_like(x)]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    yhat = A @ coef
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {"a": float(coef[0]), "b": float(coef[1]), "r2": r2,
            "residuals": (y - yhat).tolist()}
