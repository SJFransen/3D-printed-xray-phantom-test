"""Shared analysis context and ROI geometry builders.

All module APIs follow the propose/compute split required by the wizard:
  propose(ctx)            -> geometry proposal (JSON-serializable, mm + px)
  compute(ctx, geometry)  -> results computed from (possibly user-adjusted) geometry
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..features import roi_stats_annulus, roi_stats_circle, roi_stats_rect
from ..phantom_def import PhantomDef
from ..registration import Registration, Transform


@dataclass
class Ctx:
    pixels: np.ndarray
    T: Transform
    pdef: PhantomDef
    reg: Registration | None = None
    params: dict | None = None
    templates: dict | None = None

    def param(self, key, default=None):
        return (self.params or {}).get(key, default)


def img_angle_deg(T: Transform, angle_mm_deg: float) -> float:
    """Phantom-frame angle mapped into image space."""
    a = np.deg2rad(angle_mm_deg)
    v = T.A @ np.array([np.cos(a), np.sin(a)])
    return float(np.degrees(np.arctan2(v[1], v[0])))


def rect_roi(ctx: Ctx, center_mm, size_mm, angle_deg: float = 0.0,
             roi_id: str = "") -> dict:
    T = ctx.T
    c_px = np.asarray(T.mm_to_px(center_mm), float)
    ppm = T.px_per_mm
    a_img = img_angle_deg(T, angle_deg)
    w, h = size_mm[0] * ppm, size_mm[1] * ppm
    ar = np.deg2rad(a_img)
    R = np.array([[np.cos(ar), -np.sin(ar)], [np.sin(ar), np.cos(ar)]])
    local = np.array([[-w / 2, -h / 2], [w / 2, -h / 2],
                      [w / 2, h / 2], [-w / 2, h / 2]])
    corners = local @ R.T + c_px
    return {
        "id": roi_id, "type": "rect",
        "center_mm": [float(center_mm[0]), float(center_mm[1])],
        "size_mm": [float(size_mm[0]), float(size_mm[1])],
        "angle_deg": float(angle_deg),
        "center_px": c_px.tolist(), "angle_img_deg": a_img,
        "size_px": [float(w), float(h)],
        "corners_px": corners.tolist(),
    }


def circle_roi(ctx: Ctx, center_mm, dia_mm: float, roi_id: str = "") -> dict:
    T = ctx.T
    c_px = np.asarray(T.mm_to_px(center_mm), float)
    return {
        "id": roi_id, "type": "circle",
        "center_mm": [float(center_mm[0]), float(center_mm[1])],
        "dia_mm": float(dia_mm),
        "center_px": c_px.tolist(),
        "radius_px": float(dia_mm / 2 * T.px_per_mm),
    }


def annulus_roi(ctx: Ctx, center_mm, inner_dia_mm: float, outer_dia_mm: float,
                roi_id: str = "") -> dict:
    """Ring-shaped ROI. Used for local background around a low-contrast disc:
    it is unmistakably tied to its own object and needs no separate marker
    somewhere else on the phantom."""
    T = ctx.T
    c_px = np.asarray(T.mm_to_px(center_mm), float)
    return {
        "id": roi_id, "type": "annulus",
        "center_mm": [float(center_mm[0]), float(center_mm[1])],
        "inner_dia_mm": float(inner_dia_mm), "outer_dia_mm": float(outer_dia_mm),
        "center_px": c_px.tolist(),
        "inner_radius_px": float(inner_dia_mm / 2 * T.px_per_mm),
        "outer_radius_px": float(outer_dia_mm / 2 * T.px_per_mm),
    }


def segment(ctx: Ctx, p0_mm, p1_mm, seg_id: str = "") -> dict:
    T = ctx.T
    return {
        "id": seg_id, "type": "segment",
        "p0_mm": [float(p0_mm[0]), float(p0_mm[1])],
        "p1_mm": [float(p1_mm[0]), float(p1_mm[1])],
        "p0_px": np.asarray(T.mm_to_px(p0_mm), float).tolist(),
        "p1_px": np.asarray(T.mm_to_px(p1_mm), float).tolist(),
    }


def stats_for_roi(ctx: Ctx, roi: dict) -> dict:
    """Pixel statistics for a rect/circle/annulus ROI dict (uses px geometry)."""
    if roi["type"] == "circle":
        return roi_stats_circle(ctx.pixels, roi["center_px"], roi["radius_px"])
    if roi["type"] == "rect":
        return roi_stats_rect(ctx.pixels, roi["center_px"], roi["size_px"],
                              roi["angle_img_deg"])
    if roi["type"] == "annulus":
        return roi_stats_annulus(ctx.pixels, roi["center_px"],
                                 roi["inner_radius_px"], roi["outer_radius_px"])
    raise ValueError(f"unsupported ROI type {roi['type']}")


def refresh_px_geometry(ctx: Ctx, roi: dict) -> dict:
    """Recompute px fields from mm fields (after a mm-space edit)."""
    if roi["type"] == "rect":
        return {**roi, **rect_roi(ctx, roi["center_mm"], roi["size_mm"],
                                  roi["angle_deg"], roi.get("id", ""))}
    if roi["type"] == "circle":
        return {**roi, **circle_roi(ctx, roi["center_mm"], roi["dia_mm"],
                                    roi.get("id", ""))}
    if roi["type"] == "annulus":
        return {**roi, **annulus_roi(ctx, roi["center_mm"],
                                     roi["inner_dia_mm"], roi["outer_dia_mm"],
                                     roi.get("id", ""))}
    if roi["type"] == "segment":
        return {**roi, **segment(ctx, roi["p0_mm"], roi["p1_mm"],
                                 roi.get("id", ""))}
    return roi


def roi_center_mm(roi: dict):
    """Centre of any ROI type in phantom mm, including segments."""
    if roi.get("type") == "segment":
        p0, p1 = roi["p0_mm"], roi["p1_mm"]
        return [(p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0]
    return list(roi.get("center_mm", []))


def roi_center_from_px(ctx: Ctx, roi: dict, new_center_px) -> dict:
    """Apply a UI drag (new centre in px) to an ROI: update mm, refresh px.

    Segments are translated as a whole so their length and direction survive
    the move; every other type simply gets a new centre."""
    mm = ctx.T.px_to_mm(new_center_px)
    r = dict(roi)
    if r.get("type") == "segment":
        old = roi_center_mm(roi)
        dx, dy = float(mm[0]) - old[0], float(mm[1]) - old[1]
        r["p0_mm"] = [roi["p0_mm"][0] + dx, roi["p0_mm"][1] + dy]
        r["p1_mm"] = [roi["p1_mm"][0] + dx, roi["p1_mm"][1] + dy]
    else:
        r["center_mm"] = [float(mm[0]), float(mm[1])]
    return refresh_px_geometry(ctx, r)


def roi_translate_mm(ctx: Ctx, roi: dict, dx_mm: float, dy_mm: float) -> dict:
    """Move an ROI by a phantom-frame offset, whatever its type."""
    r = dict(roi)
    if r.get("type") == "segment":
        r["p0_mm"] = [roi["p0_mm"][0] + dx_mm, roi["p0_mm"][1] + dy_mm]
        r["p1_mm"] = [roi["p1_mm"][0] + dx_mm, roi["p1_mm"][1] + dy_mm]
    elif "center_mm" in r:
        r["center_mm"] = [roi["center_mm"][0] + dx_mm,
                          roi["center_mm"][1] + dy_mm]
    return refresh_px_geometry(ctx, r)


def roi_rotate(ctx: Ctx, roi: dict, angle_deg: float) -> dict:
    """Set the absolute phantom-frame angle of a rotatable ROI.

    Rectangles carry their own angle. Segments are rotated about their own
    centre, keeping their length — that is what lets a user line a profile up
    with a pattern the automatic direction search got wrong."""
    r = dict(roi)
    if r.get("type") == "rect":
        r["angle_deg"] = float(angle_deg)
        return refresh_px_geometry(ctx, r)
    if r.get("type") == "segment":
        c = roi_center_mm(roi)
        p0 = np.asarray(roi["p0_mm"], float)
        p1 = np.asarray(roi["p1_mm"], float)
        half = float(np.linalg.norm(p1 - p0)) / 2.0
        a = np.deg2rad(float(angle_deg))
        u = np.array([np.cos(a), np.sin(a)])
        r["p0_mm"] = [c[0] - half * u[0], c[1] - half * u[1]]
        r["p1_mm"] = [c[0] + half * u[0], c[1] + half * u[1]]
        return refresh_px_geometry(ctx, r)
    return roi                      # circles and annuli have no orientation


def roi_angle_deg(roi: dict) -> float | None:
    """Current phantom-frame angle, or None for shapes without one."""
    if roi.get("type") == "rect":
        return float(roi.get("angle_deg", 0.0))
    if roi.get("type") == "segment":
        p0 = np.asarray(roi["p0_mm"], float)
        p1 = np.asarray(roi["p1_mm"], float)
        d = p1 - p0
        return float(np.degrees(np.arctan2(d[1], d[0])))
    return None
