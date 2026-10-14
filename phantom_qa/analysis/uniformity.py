"""Uniformity test: 5 printed squares (center + 4 corners), SNR consistency.

Guide: SNR = mu/sigma per ROI; relative deviation of each SNR from the average
should be reasonably small (default tolerance 20 %).
"""

from __future__ import annotations

import numpy as np

from ..features import locate_line_center
from .common import Ctx, rect_roi, stats_for_roi
from .positioning import OutlineMatcher, rectangle_outline, rotation, template_for


def _refine_square(ctx: Ctx, center_mm, size_mm: float, angle_deg=0.0):
    """Locate the 4 outline lines of a printed square and return the refined
    center (mm) plus measured size, or None if detection is too weak."""
    T = ctx.T
    ppm = T.px_per_mm
    half = size_mm / 2.0
    R = rotation(angle_deg)
    found = {}
    # (key, axis unit vector along which the line position varies, sample offsets axis)
    for key, line_pos, axis in (("left", (-half, 0), (1, 0)),
                                ("right", (half, 0), (1, 0)),
                                ("bottom", (0, -half), (0, 1)),
                                ("top", (0, half), (0, 1))):
        line_pos = R @ np.asarray(line_pos, float)
        axis = R @ np.asarray(axis, float)
        perp = np.array([-axis[1], axis[0]])
        d_px = T.mm_to_px(axis) - T.mm_to_px([0, 0])
        d_px = d_px / np.linalg.norm(d_px)
        positions = []
        for off in (-size_mm * 0.25, 0.0, size_mm * 0.25):
            p_mm = (np.asarray(center_mm) + np.asarray(line_pos)
                    + off * perp)
            p_px = T.mm_to_px(p_mm)
            pt, strength = locate_line_center(
                ctx.pixels, p_px, d_px, search_half=4.0 * ppm,
                avg_half_width=1.5 * ppm, n_avg=3)
            if pt is not None:
                proj = float(np.dot(np.asarray(T.px_to_mm(pt)) - np.asarray(center_mm),
                                    axis))
                positions.append(proj)
        if len(positions) >= 2:
            found[key] = float(np.median(positions))
    if not all(k in found for k in ("left", "right", "top", "bottom")):
        return None
    center = np.asarray(center_mm) + R @ np.array([
        (found["left"] + found["right"]) / 2.0,
        (found["bottom"] + found["top"]) / 2.0])
    return {
        "center_mm": center.tolist(), "angle_deg": float(angle_deg),
        "measured_w_mm": float(found["right"] - found["left"]),
        "measured_h_mm": float(found["top"] - found["bottom"]),
    }


def propose(ctx: Ctx) -> dict:
    u = ctx.pdef.uniformity
    roi_size = u.get("roi_size_mm", 30.0)
    squares = []
    for sq in u["squares"]:
        template = template_for(ctx, "uniformity/" + sq["id"])
        points, normals, sections = rectangle_outline(template["size_mm"])
        matcher = OutlineMatcher(ctx, template, points, normals, sections)
        positioning = matcher.fit(template, required_sections=4)
        refined = (_refine_square(ctx, positioning["center_mm"], u["size_mm"],
                                  positioning["angle_deg"])
                   if positioning["accepted"] else None)
        if refined and np.linalg.norm(np.asarray(refined["center_mm"]) - sq["center_mm"]) > template["search_mm"]:
            refined = None
        if not refined:
            positioning["accepted"] = False
            positioning["reason"] = "square outlines could not be verified inside the local mask"
        else:
            positioning["refined_center_mm"] = refined["center_mm"]
        center = refined["center_mm"] if refined else sq["center_mm"]
        angle = positioning["angle_deg"] if refined else 0.0
        entry = {
            "id": sq["id"],
            "nominal_center_mm": sq["center_mm"],
            "detected": refined is not None,
            "outline": refined,
            "positioning": positioning,
            "roi": rect_roi(ctx, center, (roi_size, roi_size), angle,
                            roi_id=f"uniformity/{sq['id']}"),
        }
        squares.append(entry)
    return {"squares": squares, "roi_size_mm": roi_size}


def compute(ctx: Ctx, geometry: dict) -> dict:
    tol = ctx.pdef.tolerances.get("uniformity_dsnr_pct", 20.0)
    rows = []
    for sq in geometry["squares"]:
        s = stats_for_roi(ctx, sq["roi"])
        snr = s["mean"] / s["std"] if s["std"] > 0 else float("nan")
        rows.append({"id": sq["id"], "mean": s["mean"], "std": s["std"],
                     "n": s["n"], "snr": snr})
    snrs = np.array([r["snr"] for r in rows], float)
    means = np.array([r["mean"] for r in rows], float)
    snr_avg = float(np.nanmean(snrs))
    mean_avg = float(np.nanmean(means))
    worst = 0.0
    for r in rows:
        r["dsnr_pct"] = 100.0 * (r["snr"] - snr_avg) / snr_avg
        r["dmean_pct"] = 100.0 * (r["mean"] - mean_avg) / mean_avg
        r["status"] = "pass" if abs(r["dsnr_pct"]) <= tol else "fail"
        worst = max(worst, abs(r["dsnr_pct"]))
    status = "pass" if worst <= tol else "fail"
    bad = [r for r in rows if r["status"] != "pass"]
    if status == "pass":
        reasons = [f"every square is within +/-{tol:g}% of the mean SNR "
                   f"(worst {worst:.1f}%)."]
    else:
        reasons = [f"{r['id']}: SNR {r['snr']:.1f} is {r['dsnr_pct']:+.1f}% from "
                   f"the scan mean of {snr_avg:.1f} (tolerance +/-{tol:g}%)."
                   for r in bad]
        reasons.append("A single deviating corner usually means the ROI is not "
                       "inside its printed square; a consistent gradient across "
                       "several means genuine non-uniformity.")
    return {
        "rows": rows, "snr_avg": snr_avg, "mean_avg": mean_avg,
        "max_abs_dsnr_pct": worst, "tolerance_pct": tol,
        "status": status, "reasons": reasons,
        "affected": [r["id"] for r in bad],
    }
