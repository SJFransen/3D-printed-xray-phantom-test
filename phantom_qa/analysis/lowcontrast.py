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
from .positioning import OutlineMatcher, project, rectangle_outline, rotation, template_for


class _BrightBorderMatcher(OutlineMatcher):
    """Fine block alignment by summed brightness on the rectangle perimeter.

    A thick bright ridge has two intensity transitions. Scoring the pixels on
    the outline itself places it on the ridge, rather than on either transition.
    Contrast against both flanks verifies that a bright border actually exists.
    """

    def __init__(self, ctx, template):
        samples = max(21, int(np.ceil(max(template["size_mm"]) / 0.5)))
        points, normals, sections = rectangle_outline(template["size_mm"], samples)
        super().__init__(ctx, template, points, normals, sections)
        width, height = template["size_mm"]
        self.weights = np.array([height, height, width, width])[sections] * 0.7 / samples

    def score(self, center, angle, level):
        points = project(self.points, center, angle)
        if not np.all(self.sample(self.valid, points) > 0.99):
            return -np.inf
        return float(self.sample(self.images[level], points) @ self.weights)

    def responses(self, center, angle, level):
        points = project(self.points, center, angle)
        normals = self.normals @ rotation(angle).T
        inside, outside = points - normals * 4.0, points + normals * 4.0
        coverage = np.minimum(self.sample(self.valid, inside), self.sample(self.valid, outside))
        image = self.images[level]
        ridge = self.sample(image, points) - np.maximum(
            self.sample(image, inside), self.sample(image, outside))
        return np.array([float(np.mean(ridge[self.sections == side])) / self.noise
                         if np.all(coverage[self.sections == side] > 0.99) else -np.inf
                         for side in self.labels])


def _refine_block(ctx: Ctx):
    """Fit the block outline from its projected template, preserving its size."""
    template = template_for(ctx, "lowcontrast/block")
    points, normals, sections = rectangle_outline(template["size_mm"])
    matcher = OutlineMatcher(ctx, template, points, normals, sections, "edge")
    result = matcher.fit(template, required_sections=4)
    return result["center_mm"], result["angle_deg"], result


def _refine_bright_border(ctx: Ctx, center, angle):
    """Small final outline correction, independent of the disc-grid pose."""
    template = {**template_for(ctx, "lowcontrast/block"),
                "center_mm": list(center), "angle_deg": float(angle),
                "search_mm": 3.0, "angle_limit_deg": 3.0}
    result = _BrightBorderMatcher(ctx, template).fit(template, required_sections=4)
    result["score_type"] = "perimeter_brightness_sum"
    return result


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
    block_center, block_angle = center, ang
    grid_shift = [du_med, dv_med]
    grid_angle_offset = 0.0
    if detected:
        border = _refine_bright_border(ctx, center, ang)
        positioning["bright_border"] = border
        if border["accepted"]:
            block_center, block_angle = border["center_mm"], border["angle_deg"]
            positioning["refined_center_mm"] = block_center
            positioning["refined_angle_deg"] = block_angle
            # The bright contour may be offset from the body. Express the
            # existing disc grid relative to the new outline without moving it.
            grid_center = np.asarray(center) + du_med * u + dv_med * v
            grid_shift = ((grid_center - block_center) @ rotation(block_angle)).tolist()
            grid_angle_offset = float((ang - block_angle + 90) % 180 - 90)
    block = rect_roi(ctx, block_center, (lc["size_mm"][0], lc["size_mm"][1]), block_angle,
                     roi_id="lowcontrast/block")
    return {"block": block, "angle_deg": block_angle, "detected": detected,
            "positioning": positioning, "grid_positioning": grid_positioning,
            "grid_shift_mm": grid_shift, "grid_angle_offset_deg": grid_angle_offset,
            "circles": circles,
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
                      shift_mm=(0.0, 0.0), grid_angle_offset_deg=0.0) -> list[dict]:
    """Rebuild the eight circle ROIs from a block position and angle.

    The circles sit on a rigid grid in the block's own frame, so once the user
    has placed the block rectangle correctly every circle follows from it. This
    is what makes the block draggable and rotatable as a whole: one correction
    instead of eight. ``shift_mm`` is expressed in the block's frame; the optional
    angular offset preserves a disc grid fitted independently of its bright
    enclosing contour."""
    lc = ctx.pdef.lowcontrast
    a = np.deg2rad(float(angle_deg) + float(grid_angle_offset_deg))
    u = np.array([np.cos(a), np.sin(a)])
    v = np.array([-np.sin(a), np.cos(a)])
    dia = lc["circle_dia_mm"]
    roi_dia = lc.get("roi_dia_mm", 7.0)
    bg_in = lc.get("bg_inner_dia_mm", 12.0)
    bg_out = lc.get("bg_outer_dia_mm", 16.0)
    du, dv = float(shift_mm[0]), float(shift_mm[1])
    grid_center = np.asarray(center_mm, float) + rotation(angle_deg) @ [du, dv]

    out = []
    for c in lc["circles"]:
        cc = grid_center + c["u_mm"] * u + c["v_mm"] * v
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
