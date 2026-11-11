"""Report-only phantom-frame pictures and disposable atomic disk cache.

Adapted from dev_silvester 69db7d6. All pictures are lossless PNG; the
low-contrast crop is unprocessed. No measurement or viewer path is changed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import tempfile

import numpy as np
from scipy.ndimage import map_coordinates

from .webapp.display import window_image
from .logging_setup import get_logger

log = get_logger("thumbnails")

#: Part of every cache key. Bump it whenever the same inputs would now produce
#: a different picture — a new region, another size, another encoding — so
#: pictures kept on disk from the old code are re-rendered rather than served.
#: 2: the unrounded values are kept, not finished pictures on their own window.
PICTURE_VERSION = 3

#: Row order in the comparison table.
REGIONS = ("phantom", "linepairs", "wedge", "lowcontrast", "uniformity")

#: What an empty cell says, and why. Plain words: these land in the report.
NOT_REGISTERED = "could not be registered — no picture"
NO_BLOCK = "no measuring points stored — no picture"
UNREADABLE = "the stored scan could not be read — no picture"
FAILED = "the picture could not be made"

# Report dimensions adapted from Silvester. Lossless PNG has no fixed byte
# budget: visual fidelity takes precedence over a particular download size.

#: The whole face. Enough to see orientation, the objects and gross faults;
#: no measurement is judged from it.
PHANTOM_WIDTH_PX = 300
PHANTOM_MARGIN_MM = 8.0

#: The finest group is 2.0 lp/mm, a 0.5 mm pitch. Five samples per millimetre
#: is two and a half per line pair — enough for the lines to be seen as lines
#: in the enlarged picture, where a coarser grid would turn them into moiré.
LINEPAIR_PX_PER_MM = 5.0
#: Across the strip. The blocks sit a few millimetres off the line through
#: their design centres on some prints, so the corridor is wide enough to hold
#: them and the frame line either side on every reference scan.
LINEPAIR_CORRIDOR_MM = 30.0

#: The steps are about 19 mm tall, so two samples per millimetre shows every
#: step and its edges; the frame either side of the column is included.
WEDGE_PX_PER_MM = 2.0
WEDGE_WIDTH_MM = 36.0
WEDGE_MARGIN_MM = 6.0

#: A square and a little of its surround, so its printed outline shows.
UNIFORMITY_PX_PER_MM = 1.2
UNIFORMITY_MARGIN_MM = 3.0
UNIFORMITY_GAP_PX = 3
#: Reading order across the face. Anything else a definition names follows.
UNIFORMITY_ORDER = ("TL", "TR", "C", "BL", "BR")

#: Unprocessed low-contrast comparison crop width.
LOWCONTRAST_WIDTH_PX = 280

#: A region's own window, generous so that a scan alone on its detector and
#: protocol — shown on its own window — keeps its highlights and shadows.
WINDOW_PERCENTILES = (0.2, 99.8)


# --------------------------------------------------------------- sampling

class _Source:
    """The scan at the coarser resolutions the pictures need.

    Sampling a 7 px/mm detector at 1 px/mm point by point picks single noisy
    pixels: the picture looks grainier than the scan, and noise is the most
    expensive thing an image encoder can be asked to hold. Averaging whole blocks first
    is the cheap anti-aliasing filter — a reshape, not a convolution — and
    each reduction is made once and shared by every region that wants it."""

    def __init__(self, ctx):
        self.pixels = ctx.pixels
        self.T = ctx.T
        self._levels: dict[int, np.ndarray] = {}

    def at(self, px_per_mm: float):
        f = max(1, int(self.T.px_per_mm / max(px_per_mm, 1e-6)))
        if f not in self._levels:
            if f == 1:
                self._levels[f] = self.pixels
            else:
                h, w = self.pixels.shape
                hh, ww = h // f, w // f
                self._levels[f] = self.pixels[:hh * f, :ww * f].reshape(
                    hh, f, ww, f).mean(axis=(1, 3))
        return self._levels[f], f


def _sample(src: _Source, centre_mm, right_mm, up_mm, width_mm: float,
            height_mm: float, px_per_mm: float) -> np.ndarray:
    """A rectangle of the phantom frame, resampled onto a picture grid.

    ``right_mm`` and ``up_mm`` are the phantom-frame directions that become
    the picture's right and up. Row 0 is the top. Anything that falls outside
    the detector comes back as NaN, so it can be painted as such rather than
    smeared from the nearest edge."""
    cols = max(int(round(width_mm * px_per_mm)), 8)
    rows = max(int(round(height_mm * px_per_mm)), 8)
    us = (np.arange(cols) + 0.5) / px_per_mm - width_mm / 2.0
    vs = height_mm / 2.0 - (np.arange(rows) + 0.5) / px_per_mm
    uu, vv = np.meshgrid(us, vs)
    pts = (np.asarray(centre_mm, float)
           + uu[..., None] * np.asarray(right_mm, float)
           + vv[..., None] * np.asarray(up_mm, float))
    px = np.atleast_2d(src.T.mm_to_px(pts.reshape(-1, 2)))
    img, f = src.at(px_per_mm)
    # A pixel of the reduced image averages an f x f block, so its centre sits
    # half a block in from the block's corner.
    x = (px[:, 0] - (f - 1) / 2.0) / f
    y = (px[:, 1] - (f - 1) / 2.0) / f
    # (row, col) = (y, x); bilinear is right for a picture meant to be looked
    # at, which is all these are.
    out = map_coordinates(img, [y, x], order=1, mode="constant", cval=np.nan,
                          output=np.float64)
    return out.reshape(rows, cols)


def _window(values: np.ndarray) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = (float(v) for v in np.percentile(finite, WINDOW_PERCENTILES))
    if not hi > lo:
        lo, hi = float(finite.min()), float(finite.max())
    if not hi > lo:
        # A flat exposure. It is still drawn — seeing that there is nothing
        # there is exactly what the viewer needs — as a mid-grey rather than
        # an error.
        lo, hi = lo - 1.0, lo + 1.0
    return lo, hi


def _from_values(values: np.ndarray, lo: float | None = None,
                 hi: float | None = None, window: str = "raw",
                 **extra) -> dict:
    """One region as what is kept of it: the unrounded values and their own
    window ``lo``..``hi``.

    With ``window="raw"`` the values are scan values, and the report may show
    the picture on another scan's window of the same protocol; ``"self"``
    marks values already normalised to themselves, whose brightness means
    nothing next to another scan's."""
    if lo is None or hi is None:
        lo, hi = _window(values)
    return {"values": np.asarray(values, dtype=np.float32),
            "lo": float(lo), "hi": float(hi), "window": window, **extra}


def drawable(region) -> bool:
    """Whether a kept region can be drawn: values and a finite window."""
    if not isinstance(region, dict):
        return False
    v = region.get("values")
    return (isinstance(v, np.ndarray) and v.ndim == 2 and v.size > 0
            and all(isinstance(region.get(k), (int, float))
                    and np.isfinite(region[k]) for k in ("lo", "hi")))


def encode_on(region: dict, lo: float, hi: float) -> dict:
    """Lossless report-only picture, using the incumbent window mapping."""
    from PIL import Image
    values = region["values"]
    a8 = window_image(np.where(np.isfinite(values), values, lo),
                      (lo + hi) / 2, hi - lo)
    buf = io.BytesIO()
    Image.fromarray(a8).save(buf, format="PNG")
    data = buf.getvalue()
    return {"mime": "image/png", "b64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data), "w": a8.shape[1], "h": a8.shape[0]}


# ---------------------------------------------------------------- regions

def _phantom(src: _Source, pdef) -> dict:
    side = float(pdef.side_mm) + 2.0 * PHANTOM_MARGIN_MM
    return _from_values(_sample(src, (0.0, 0.0), (1.0, 0.0), (0.0, 1.0),
                                side, side, PHANTOM_WIDTH_PX / side))


def _linepairs(src: _Source, pdef) -> dict:
    """A corridor along the strip, first design group at the left.

    Taken through the groups' DESIGN positions, not through where this scan's
    ROIs ended up, so every column shows the same stretch of the phantom and
    a group that was mis-placed on one scan is visible as exactly that."""
    lp = pdef.linepairs
    centres = np.array([g["center_mm"] for g in lp["groups"]], float)
    right = centres[-1] - centres[0]
    right = right / max(float(np.linalg.norm(right)), 1e-9)
    up = np.array([-right[1], right[0]])            # a turn left: no mirroring
    along = (centres - centres.mean(axis=0)) @ right
    mid = centres.mean(axis=0) + (along.min() + along.max()) / 2.0 * right
    pad = float(lp.get("roi_size_mm", 12.6))
    length = float(along.max() - along.min()) + 2.0 * pad
    return _from_values(_sample(src, mid, right, up, length,
                                LINEPAIR_CORRIDOR_MM, LINEPAIR_PX_PER_MM))


def _wedge(src: _Source, pdef) -> dict:
    """The step column on its side: S1 (the top of the phantom) at the left.

    Laid flat because a table cell is wide rather than tall. Phantom "down"
    becomes picture "right" and phantom "right" picture "up" — a quarter turn,
    not a reflection, so the picture is the phantom as it is."""
    w = pdef.wedge
    top, bottom = float(w["y_top_mm"]), float(w["y_bottom_mm"])
    centre = (float(w["center_x_mm"]), (top + bottom) / 2.0)
    length = (top - bottom) + 2.0 * WEDGE_MARGIN_MM
    return _from_values(_sample(src, centre, (0.0, -1.0), (1.0, 0.0), length,
                                WEDGE_WIDTH_MM, WEDGE_PX_PER_MM))


def _lowcontrast(ctx, geometry) -> dict:
    """Unprocessed scan crop; no new flattening or measurement algorithm."""
    block = ((geometry or {}).get("lowcontrast") or {}).get("block") or {}
    if block.get("center_mm") is None:
        return {"missing": NO_BLOCK}
    angle = np.deg2rad(float(block.get("angle_deg", 0)))
    right = (np.cos(angle), np.sin(angle))
    up = (-np.sin(angle), np.cos(angle))
    width, height = block.get("size_mm", (100, 100))
    return _from_values(_sample(_Source(ctx), block["center_mm"], right, up,
                                float(width), float(height),
                                LOWCONTRAST_WIDTH_PX / float(width)), window="self")


def _uniformity(src: _Source, pdef) -> dict:
    """The five squares in one strip, sharing one window.

    One picture rather than five, so that within a scan a square that is
    brighter than its neighbours shows as brighter — which is the question
    the uniformity test asks.

    The window comes from the middle of each square, the area the test
    measures. Taken over the whole tile it was set by the printed outlines,
    which attenuate far more than anything inside them, and every square
    came out the same flat dark grey whatever its signal."""
    u = pdef.uniformity
    squares = {s["id"]: s for s in u["squares"]}
    ids = [i for i in UNIFORMITY_ORDER if i in squares] \
        + [i for i in squares if i not in UNIFORMITY_ORDER]
    tile_mm = float(u.get("size_mm", 50.1)) + 2.0 * UNIFORMITY_MARGIN_MM
    tiles = [_sample(src, squares[i]["center_mm"], (1.0, 0.0), (0.0, 1.0),
                     tile_mm, tile_mm, UNIFORMITY_PX_PER_MM) for i in ids]
    rows, cols = tiles[0].shape
    half = int(round(float(u.get("roi_size_mm", 30.0)) / 2.0
                     * UNIFORMITY_PX_PER_MM))
    r0, c0 = max(rows // 2 - half, 0), max(cols // 2 - half, 0)
    inner = np.concatenate([t[r0:rows - r0, c0:cols - c0].ravel()
                            for t in tiles])
    gap = UNIFORMITY_GAP_PX
    strip = np.full((rows, len(tiles) * cols + (len(tiles) - 1) * gap), np.nan)
    for k, tile in enumerate(tiles):
        strip[:, k * (cols + gap):k * (cols + gap) + cols] = tile
    lo, hi = _window(inner)
    return _from_values(strip, lo, hi, labels=ids)


def render_all(ctx, geometry) -> dict:
    """Every region of one scan: a picture, or why there is none.

    One region failing never costs the others: each is made on its own, and
    a failure is recorded as a labelled gap flagged ``failed`` — which also
    keeps it out of the disk cache, so the next report tries again."""
    src = _Source(ctx)
    makers = {
        "phantom": lambda: _phantom(src, ctx.pdef),
        "linepairs": lambda: _linepairs(src, ctx.pdef),
        "wedge": lambda: _wedge(src, ctx.pdef),
        "lowcontrast": lambda: _lowcontrast(ctx, geometry),
        "uniformity": lambda: _uniformity(src, ctx.pdef),
    }
    out = {}
    for name in REGIONS:
        try:
            out[name] = makers[name]()
        except Exception:
            log.exception("comparison picture %r could not be made", name)
            out[name] = {"missing": FAILED, "failed": True}
    return out


def unavailable(reason: str) -> dict:
    """Every region as a labelled empty cell."""
    return {name: {"missing": reason} for name in REGIONS}


# ------------------------------------------------------------------ cache

def cache_key(rec: dict, *, pdef_version: str, algo_version: str) -> str:
    """Everything the pictures of one analysis depend on, as a short digest.

    geometry_seq alone is not enough: it restarts at 0 when a re-run starts
    from registration and repeats after an undo followed by a new edit, so the
    same number can stand for different measuring points. The transform and
    the block placement are therefore part of the key as well — they are what
    the pictures are actually drawn from — and the sequence number rides along
    so any change to the measuring points re-renders."""
    ident = {
        "picture_version": PICTURE_VERSION,
        "algo": algo_version, "pdef": pdef_version,
        "sha256": rec.get("sha256") or "",
        "seq": int(rec.get("geometry_seq") or 0),
        "transform": (rec.get("reg") or {}).get("transform"),
        "geometry": rec.get("geometry"),
    }
    raw = json.dumps(ident, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _read(directory: str | None, key: str) -> dict | None:
    """The regions kept under ``key``, or None for anything but a whole,
    current file. Read without pickle: the file holds arrays and one JSON
    text, and nothing in it can run code."""
    if not directory:
        return None
    try:
        with np.load(os.path.join(directory, f"{key}.npz"),
                     allow_pickle=False) as kept:
            doc = json.loads(str(kept["meta"]))
            if (not isinstance(doc, dict) or doc.get("key") != key
                    or doc.get("version") != PICTURE_VERSION
                    or not isinstance(doc.get("regions"), dict)):
                return None
            regions = {}
            for name in REGIONS:
                meta = doc["regions"].get(name)
                if not isinstance(meta, dict):
                    return None
                if "missing" in meta:
                    regions[name] = {"missing": str(meta["missing"])}
                    continue
                regions[name] = {
                    "values": np.asarray(kept[name], dtype=np.float32),
                    "lo": float(meta["lo"]), "hi": float(meta["hi"]),
                    "window": str(meta["window"]),
                    **({"labels": [str(x) for x in meta["labels"]]}
                       if meta.get("labels") else {})}
                if not drawable(regions[name]) or regions[name]["hi"] <= regions[name]["lo"]:
                    return None
    except Exception:           # missing, half-written or damaged: a miss
        return None
    return regions


def _write(directory: str, key: str, regions: dict) -> None:
    """Keep the regions under their key, and drop any older set.

    The values compressed in one file, with what else is known about each
    region as JSON beside them. Written to a temporary file and renamed into
    place, so another worker reading the same analysis at that moment sees
    either the whole file or none of it. A failure to write costs a
    re-render next time, never the page."""
    name = f"{key}.npz"
    arrays, meta = {}, {}
    for region, r in regions.items():
        if "values" in r:
            arrays[region] = r["values"]
            meta[region] = {k: r[k] for k in ("lo", "hi", "window", "labels")
                            if k in r}
        else:
            meta[region] = {"missing": r.get("missing", FAILED)}
    doc = json.dumps({"key": key, "version": PICTURE_VERSION,
                      "regions": meta})
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            np.savez_compressed(f, meta=np.array(doc), **arrays)
        os.replace(tmp, os.path.join(directory, name))
    except OSError as e:
        log.warning("comparison pictures could not be kept in %s: %s",
                    directory, e)
        return
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    # Pictures of measuring points that no longer exist are dead weight — as
    # are files from before the values were kept (.json); only finished
    # files are touched, never another worker's temporary one.
    try:
        others = os.listdir(directory)
    except OSError:
        return  # concurrent deletion or cache failure cannot break a report
    for other in others:
        if other.endswith((".json", ".npz")) and other != name:
            try:
                os.remove(os.path.join(directory, other))
            except OSError:
                pass


def forget(directory: str | None) -> None:
    """Remove every kept picture of one analysis."""
    if directory and os.path.isdir(directory):
        shutil.rmtree(directory, ignore_errors=True)


def pictures_for(rec: dict, directory: str | None, make_ctx, *,
                 pdef_version: str, algo_version: str) -> dict:
    """``{region: kept values or {"missing": reason}}`` for one stored
    analysis — draw each with :func:`encode_on`.

    ``rec`` must be a full record (registration and geometry included);
    ``make_ctx`` builds the analysis context and is only called when the
    pictures are not already kept under ``directory``. Taking it as an
    argument keeps the web layer's scan loading and caching out of here.

    Rendering needs the decoded scan, which is the expensive part — seconds
    and tens of megabytes — so a comparison reopened later, or one sharing
    scans with another, pays nothing for a scan it has shown before."""
    if not (rec.get("reg") or {}).get("transform"):
        return unavailable(NOT_REGISTERED)
    key = cache_key(rec, pdef_version=pdef_version, algo_version=algo_version)
    kept = _read(directory, key)
    if kept is not None:
        return kept
    try:
        ctx = make_ctx()
    except Exception as e:
        log.warning("comparison pictures: scan of analysis=%s could not be "
                    "loaded: %s", rec.get("id"), getattr(e, "detail", e))
        return unavailable(UNREADABLE)
    regions = render_all(ctx, rec.get("geometry"))
    if directory and not any(r.get("failed") for r in regions.values()):
        _write(directory, key, regions)
    return regions
