"""Bound field intrusion from the phantom edge using inward ruler visibility."""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .common import segment


def measure(ctx, side, ruler):
    definition = ctx.pdef.rulers[side]
    inward = np.asarray(definition["normal_inward_mm"], float)
    tangent = np.array([-inward[1], inward[0]])
    edge = -inward * ctx.pdef.side_mm / 2
    probe = (ruler or {}).get("probe") or {}
    if probe.get("p1_mm") is not None:
        edge += tangent * np.dot(np.asarray(probe["p1_mm"]), tangent)
    depths = np.arange(0., 42.01, .1)
    nominal = definition["first_line_from_edge_mm"] + np.arange(definition.get("n_lines", 6)) * definition["pitch_mm"]
    marks = []
    padding = ((ctx.params or {}).get("scan_meta") or {}).get("_padding_range_normalized")
    pixels = ctx.pixels
    usable = np.isfinite(pixels)
    if padding is not None:
        usable &= (pixels < padding[0]) | (pixels > padding[1])
    native = pixels[usable]
    dynamic_range = float(np.ptp(np.percentile(native, [1, 99]))) if native.size else 0.
    rays = []
    for shift in (-2., -1., 0., 1., 2.):
        points = ctx.T.mm_to_px(edge + depths[:, None] * inward + shift * tangent)
        inside = ((points[:, 0] >= 0) & (points[:, 0] <= pixels.shape[1] - 1)
                  & (points[:, 1] >= 0) & (points[:, 1] <= pixels.shape[0] - 1))
        values = ndimage.map_coordinates(pixels, [points[:, 1], points[:, 0]], order=1, mode="nearest")
        native_values = ndimage.map_coordinates(pixels, [points[:, 1], points[:, 0]], order=0, mode="nearest")
        valid = inside & np.isfinite(values)
        if padding is not None:
            valid &= (native_values < padding[0]) | (native_values > padding[1])
        rays.append((values, valid))
    for index, center in enumerate(nominal):
        signal = np.abs(depths - center) <= 1.3
        before = (depths >= center - 2.3) & (depths <= center - 1.7)
        after = (depths >= center + 1.7) & (depths <= center + 2.3)
        contrasts, cnrs, positions = [], [], []
        for values, valid in rays:
            if not np.all(valid[signal | before | after]):
                continue
            left, right = np.median(values[before]), np.median(values[after])
            local = ndimage.gaussian_filter1d(values[signal], 1.)
            bright, dark = np.percentile(local, [90, 10])
            high = max(0., min(bright - left, bright - right))
            low = max(0., min(left - dark, right - dark))
            contrast = max(high, low)
            residuals = np.r_[values[before] - left, values[after] - right]
            noise = max(float(1.4826 * np.median(np.abs(residuals))), 1.)
            peak = int(np.argmax(local) if high >= low else np.argmin(local))
            contrasts.append(float(contrast))
            cnrs.append(float(contrast / noise))
            positions.append(float(depths[signal][peak]))
        marks.append({"index": index, "nominal_depth_mm": float(center),
                      "contrast": float(np.median(contrasts)) if contrasts else 0.,
                      "cnr": float(np.median(cnrs)) if cnrs else 0.,
                      "measured_depth_mm": float(np.median(positions)) if positions else None,
                      "ray_contrasts": contrasts, "ray_cnrs": cnrs})
    reference = max((m["contrast"] for m in marks), default=0.)
    minimum = max(.01 * dynamic_range, .35 * reference, 1.)
    for mark in marks:
        votes = sum(c >= minimum and cnr >= 5 for c, cnr in zip(mark.pop("ray_contrasts"), mark.pop("ray_cnrs")))
        mark.update(visible=votes >= 3, agreeing_profiles=votes)
    base = {"side": side, "method": "inward_ruler_contrast", "manual": False,
            "ruler_visibility": marks, "contrast_threshold": minimum,
            "probe": segment(ctx, edge, edge + inward * 42, f"field/{side}")}
    visible = [m["index"] for m in marks if m["visible"]]
    if not visible:
        return {**base, "detected": False, "reason": "No reliably visible ruler marks; equal low contrast does not establish coverage"}
    first = visible[0]
    if any(not m["visible"] for m in marks[first:]):
        return {**base, "detected": False, "reason": "Ruler contrast does not show sustained inward visibility"}
    # Include a 1 mm localization allowance; these are bounds, not exact edges.
    lower = max(0., float(nominal[first-1]) - 1.) if first else 0.
    upper = float(nominal[first]) + 1.
    return {**base, "detected": True, "intrusion_bounds_mm": [lower, upper],
            "measurement_kind": "interval" if first else "upper_bound",
            "intrusion_segment": segment(ctx, edge + inward * lower, edge + inward * upper, f"field/{side}/bounds"),
            "offset_from_edge_mm": -upper,
            "edge_pt_px": ctx.T.mm_to_px(edge + inward * upper).tolist()}
