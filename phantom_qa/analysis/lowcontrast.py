"""Low-contrast test: 8 circles (diameter 10 mm) in the rounded-rectangle block.

Guide: CNR = (mu_obj - mu_bg) / sqrt(sigma_obj^2 + sigma_bg^2) per circle.
The circles are printed with fewer layers than the surroundings -> less
attenuating -> darker (lower value) than the block background.

No nominal contrast values are assumed: circles carry a positional design-order
``level`` (L1 = weakest ... L8 = strongest, as measured on the reference scans).
The QA criterion is constancy of each circle's CNR over time.
"""

from __future__ import annotations

import numpy as np

from .common import Ctx, annulus_roi, circle_roi, rect_roi, stats_for_roi
from .positioning import OutlineMatcher, rectangle_outline, template_for


def _refine_block(ctx: Ctx):
    """Fit the block outline from its projected template, preserving its size."""
    template = template_for(ctx, "lowcontrast/block")
    points, normals, sections = rectangle_outline(template["size_mm"])
    matcher = OutlineMatcher(ctx, template, points, normals, sections, "edge")
    result = matcher.fit(template, required_sections=4)
    return result["center_mm"], result["angle_deg"], result


def propose(ctx: Ctx) -> dict:
    lc = ctx.pdef.lowcontrast
    center, ang, positioning = _refine_block(ctx)
    detected = positioning["accepted"]
    a = np.deg2rad(ang)
    u = np.array([np.cos(a), np.sin(a)])          # long axis
    v = np.array([-np.sin(a), np.cos(a)])         # short axis
    dia = lc["circle_dia_mm"]
    roi_dia = lc.get("roi_dia_mm", 7.0)

    # Refine all circle rims as one rigid grid. Weak circles inherit the common
    # pose rather than wandering to whichever local pixels maximise their CNR.
    du_med = dv_med = 0.0
    grid_positioning = None
    if detected:
        theta = np.linspace(0, 2 * np.pi, 32, endpoint=False)
        radial = np.c_[np.cos(theta), np.sin(theta)]
        points = np.vstack([radial * dia / 2 + [c["u_mm"], c["v_mm"]]
                            for c in lc["circles"]])
        normals = np.tile(radial, (len(lc["circles"]), 1))
        sections = np.repeat(np.arange(len(lc["circles"])), len(theta))
        template = {**template_for(ctx, "lowcontrast/block"),
                    "center_mm": center, "angle_deg": ang,
                    "search_mm": 2.5, "angle_limit_deg": 4.0}
        matcher = OutlineMatcher(ctx, template, points, normals, sections, "dark_edge")
        # Each section averages an entire circumference; coherent rim evidence
        # can therefore be smaller than the noise of individual local pixels.
        grid_positioning = matcher.fit(template, required_sections=3, min_section_score=0.75)
        if grid_positioning["accepted"]:
            ang = grid_positioning["angle_deg"]
            a = np.deg2rad(ang)
            u = np.array([np.cos(a), np.sin(a)])
            v = np.array([-np.sin(a), np.cos(a)])
            delta = np.asarray(grid_positioning["center_mm"]) - center
            du_med, dv_med = float(delta @ u), float(delta @ v)

    bg_in = lc.get("bg_inner_dia_mm", 12.0)
    bg_out = lc.get("bg_outer_dia_mm", 16.0)
    circles = []
    for c in lc["circles"]:
        cc = (np.asarray(center) + (c["u_mm"] + du_med) * u
              + (c["v_mm"] + dv_med) * v)
        circles.append({
            "id": c["id"], "level": c["level"],
            "roi": circle_roi(ctx, cc, roi_dia, roi_id=f"lowcontrast/{c['id']}"),
            # Local background is the ring around this circle. An ROI placed
            # elsewhere on the block (e.g. on the midline) both picks up the
            # block's own brightness gradient and shows up as an unexplained
            # marker in the middle of the object.
            "bg_roi": annulus_roi(ctx, cc, bg_in, bg_out,
                                  roi_id=f"lowcontrast/{c['id']}/bg"),
            "full_circle": circle_roi(ctx, cc, dia,
                                      roi_id=f"lowcontrast/{c['id']}/outline"),
        })
    block = rect_roi(ctx, center, (lc["size_mm"][0], lc["size_mm"][1]), ang,
                     roi_id="lowcontrast/block")
    return {"block": block, "angle_deg": ang, "detected": detected,
            "positioning": positioning, "grid_positioning": grid_positioning,
            "grid_shift_mm": [du_med, dv_med], "circles": circles,
            "bg_ring_mm": [bg_in, bg_out]}


def compute(ctx: Ctx, geometry: dict) -> dict:
    rows = []
    for c in geometry["circles"]:
        so = stats_for_roi(ctx, c["roi"])
        sb = stats_for_roi(ctx, c["bg_roi"])
        denom = float(np.sqrt(so["std"] ** 2 + sb["std"] ** 2))
        cnr = (so["mean"] - sb["mean"]) / denom if denom > 0 else float("nan")
        rows.append({
            "id": c["id"], "level": c["level"],
            "obj_mean": so["mean"], "obj_std": so["std"], "obj_n": so["n"],
            "bg_mean": sb["mean"], "bg_std": sb["std"], "bg_n": sb["n"],
            "cnr": float(cnr), "abs_cnr": float(abs(cnr)),
        })
    rows.sort(key=lambda r: r["level"])
    # sanity: |CNR| should broadly increase with the design-order level
    cnrs = [r["abs_cnr"] for r in rows]
    increasing = sum(1 for i in range(len(cnrs) - 1) if cnrs[i + 1] >= cnrs[i])
    ordering_ok = increasing >= max(len(cnrs) - 3, 1)
    weak = [r["id"] for r in rows if abs(r["cnr"]) < 0.2]
    if ordering_ok:
        reasons = ["|CNR| increases with the design order of the circles, so "
                   "the grid is on the right objects."]
    else:
        order = ' < '.join(r['id'] for r in rows)
        reasons = [f"|CNR| does not increase with the circles' design order "
                   f"({order} should run weakest to strongest). The usual "
                   f"cause is that the block or the circle grid is not on the "
                   f"printed objects — reposition the block in step C."]
    if weak:
        reasons.append(f"Very low contrast on {', '.join(weak)} "
                       f"(|CNR| < 0.2): either genuinely at the limit of "
                       f"detectability, or the ROI is off the disc.")
    return {"rows": rows, "ordering_ok": bool(ordering_ok),
            "status": "pass" if ordering_ok else "warn",
            "reasons": reasons, "affected": weak}


def circles_for_block(ctx: Ctx, center_mm, angle_deg: float,
                      shift_mm=(0.0, 0.0)) -> list[dict]:
    """Rebuild the eight circle ROIs from a block position and angle.

    The circles sit on a rigid grid in the block's own frame, so once the user
    has placed the block rectangle correctly every circle follows from it. This
    is what makes the block draggable and rotatable as a whole: one correction
    instead of eight."""
    lc = ctx.pdef.lowcontrast
    a = np.deg2rad(float(angle_deg))
    u = np.array([np.cos(a), np.sin(a)])
    v = np.array([-np.sin(a), np.cos(a)])
    dia = lc["circle_dia_mm"]
    roi_dia = lc.get("roi_dia_mm", 7.0)
    bg_in = lc.get("bg_inner_dia_mm", 12.0)
    bg_out = lc.get("bg_outer_dia_mm", 16.0)
    du, dv = float(shift_mm[0]), float(shift_mm[1])

    out = []
    for c in lc["circles"]:
        cc = (np.asarray(center_mm, float) + (c["u_mm"] + du) * u
              + (c["v_mm"] + dv) * v)
        out.append({
            "id": c["id"], "level": c["level"],
            "roi": circle_roi(ctx, cc, roi_dia, roi_id=f"lowcontrast/{c['id']}"),
            "bg_roi": annulus_roi(ctx, cc, bg_in, bg_out,
                                  roi_id=f"lowcontrast/{c['id']}/bg"),
            "full_circle": circle_roi(ctx, cc, dia,
                                      roi_id=f"lowcontrast/{c['id']}/outline"),
        })
    return out


def block_from_corners(ctx: Ctx, corners_px) -> tuple[list, float]:
    """Centre and angle of the block from four user-clicked corners.

    Click order does not matter: the long axis is taken from the longer pair of
    opposite edge midpoints, mirroring how the phantom outline is fitted."""
    pts = np.array([ctx.T.px_to_mm(p) for p in corners_px], float)
    if len(pts) != 4:
        raise ValueError("exactly four corners are required")
    centre = pts.mean(axis=0)
    ordered = pts[np.argsort(np.arctan2(pts[:, 1] - centre[1],
                                        pts[:, 0] - centre[0]))]
    mids = [(ordered[i] + ordered[(i + 1) % 4]) / 2.0 for i in range(4)]
    axis_a = mids[2] - mids[0]
    axis_b = mids[3] - mids[1]
    axis = axis_a if np.linalg.norm(axis_a) >= np.linalg.norm(axis_b) else axis_b
    angle = float(np.degrees(np.arctan2(axis[1], axis[0])))
    if angle > 90:
        angle -= 180
    if angle < -90:
        angle += 180
    return [float(centre[0]), float(centre[1])], angle
