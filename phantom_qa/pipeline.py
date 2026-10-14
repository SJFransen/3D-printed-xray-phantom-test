"""Pipeline orchestration: wizard stages as pure functions over ScanData.

Stage A: register()                -> registration + transform
Stage B/C: propose_all()           -> per-test geometry proposals (editable)
Stage D/E: compute_all()           -> results from (possibly adjusted) geometry
Overlays: render_overlay()         -> annotated PNG for reports/UI snapshots
"""

from __future__ import annotations

import io

import numpy as np

from . import ALGO_VERSION
from .analysis import geometry as geo_mod
from .analysis import linepairs, lowcontrast, uniformity, wedge
from .analysis.assessment import TESTS, evaluate_results
from .analysis.common import Ctx
from .analysis.positioning import build_templates
from .ingest import ScanData, protocol_signature
from .phantom_def import PhantomDef
from .registration import Registration, register

_MODULES = {"geometry": geo_mod, "linepairs": linepairs,
            "lowcontrast": lowcontrast, "uniformity": uniformity,
            "wedge": wedge}


def to_jsonable(obj):
    """Recursively convert numpy types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return v if np.isfinite(v) else None
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def build_ctx(scan: ScanData, pdef: PhantomDef, reg: Registration,
              params: dict | None = None) -> Ctx:
    p = {"spacing_candidates": scan.spacing_candidates}
    if params:
        p.update(params)
    return Ctx(pixels=scan.pixels, T=reg.transform, pdef=pdef, reg=reg, params=p)


def run_stage_a(scan: ScanData, pdef: PhantomDef,
                corners_hint=None) -> Registration:
    return register(scan.pixels, pdef, corners_hint=corners_hint)


def registration_to_dict(reg: Registration) -> dict:
    return to_jsonable({
        "transform": reg.transform.to_dict(),
        "corners_px": reg.corners_px,
        "coarse_angle_deg": reg.coarse_angle_deg,
        "score": reg.score,
        "candidate_scores": reg.candidate_scores,
        "landmarks": reg.landmarks,
        "residual_rms_mm": reg.residual_rms_mm,
    })


def registration_from_dict(d: dict) -> Registration:
    """Rebuild a stored registration, so results can be recomputed from the
    geometry the user confirmed instead of detecting it again."""
    from .registration import Transform
    return Registration(
        transform=Transform.from_dict(d["transform"]),
        corners_px=np.asarray(d["corners_px"], float),
        coarse_angle_deg=d.get("coarse_angle_deg", float("nan")),
        score=d.get("score", {}),
        candidate_scores=d.get("candidate_scores", []),
        landmarks=d.get("landmarks", {}),
        residual_rms_mm=d.get("residual_rms_mm", float("nan")),
    )


def propose_all(ctx: Ctx) -> dict:
    # Project every feature before any local refinement. The registered frame
    # is shared; independent local corrections never alter the measurement scale.
    ctx.templates = build_templates(ctx)
    out = {}
    for name in TESTS:
        try:
            out[name] = _MODULES[name].propose(ctx)
            out[name]["_error"] = None
        except Exception as e:  # keep the wizard usable when one test fails
            out[name] = {"_error": f"{type(e).__name__}: {e}"}
    return to_jsonable(out)


def compute_all(ctx: Ctx, geometry_all: dict) -> dict:
    out = {}
    for name in TESTS:
        geom = geometry_all.get(name)
        if not geom or geom.get("_error"):
            out[name] = {"status": "n/a",
                         "error": (geom or {}).get("_error", "no geometry")}
            continue
        try:
            out[name] = _MODULES[name].compute(ctx, geom)
        except Exception as e:
            out[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    # `params` is optional on Ctx and every other reader goes through
    # ctx.param(); dereferencing it directly turned a context built without one
    # into an AttributeError 500 rather than a result.
    params = ctx.params or {}
    out["_meta"] = {
        "algo_version": ALGO_VERSION,
        "signature": protocol_signature(params.get("scan_meta", {}))
        if params.get("scan_meta") else None,
        "registration": ctx.reg.summary() if ctx.reg else None,
    }
    checked, _ = evaluate_results(geometry_all, to_jsonable(out), ctx.pdef)
    return to_jsonable(checked)


def overall_status(results: dict) -> str:
    order = {"pass": 0, "n/a": 1, "warn": 2, "fail": 3, "error": 3}
    worst = "pass"
    for name in TESTS:
        r = results.get(name, {})
        for key in ("status", "field_status", "dimension_status"):
            s = r.get(key)
            if s and order.get(s, 0) > order.get(worst, 0):
                worst = s
    return worst


# ------------------------------------------------------------------ overlays

_COLORS = {"geometry": "#00c8ff", "linepairs": "#ffd400",
           "lowcontrast": "#ff7bda", "uniformity": "#7bff9f",
           "wedge": "#ff9d5c", "reg": "#ff4040"}


def render_overlay(scan: ScanData, ctx: Ctx, geometry_all: dict,
                   max_px: int = 1400) -> bytes:
    """Annotated overview PNG: image + registration corners + all ROIs."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle as MplCircle
    from matplotlib.patches import Polygon as MplPolygon

    img = scan.pixels
    ds = max(1, int(np.ceil(max(img.shape) / max_px)))
    small = img[::ds, ::ds]
    lo, hi = np.percentile(small, [1, 99])

    fig, axp = plt.subplots(figsize=(11, 11), dpi=120)
    axp.imshow(small, cmap="gray", vmin=lo, vmax=hi,
               extent=(0, img.shape[1], img.shape[0], 0))
    axp.set_axis_off()

    if ctx.reg is not None:
        c = np.vstack([ctx.reg.corners_px, ctx.reg.corners_px[:1]])
        axp.plot(c[:, 0], c[:, 1], color=_COLORS["reg"], lw=1.2, ls="--")

    def draw_roi(roi, color):
        if not isinstance(roi, dict):
            return
        t = roi.get("type")
        if t == "rect":
            axp.add_patch(MplPolygon(np.asarray(roi["corners_px"]), closed=True,
                                     fill=False, edgecolor=color, lw=1.0))
        elif t == "circle":
            axp.add_patch(MplCircle(roi["center_px"], roi["radius_px"],
                                    fill=False, edgecolor=color, lw=1.0))
        elif t == "annulus":
            for r in (roi["inner_radius_px"], roi["outer_radius_px"]):
                axp.add_patch(MplCircle(roi["center_px"], r, fill=False,
                                        edgecolor=color, lw=0.6, ls=":"))
        elif t == "segment":
            p0, p1 = np.asarray(roi["p0_px"]), np.asarray(roi["p1_px"])
            axp.plot([p0[0], p1[0]], [p0[1], p1[1]], color=color, lw=0.8)

    def walk(node, color):
        if isinstance(node, dict):
            if node.get("type") in ("rect", "circle", "segment", "annulus"):
                draw_roi(node, color)
            else:
                for v in node.values():
                    walk(v, color)
        elif isinstance(node, list):
            for v in node:
                walk(v, color)

    for name in TESTS:
        g = geometry_all.get(name)
        if g and not g.get("_error"):
            walk(g, _COLORS[name])

    buf = io.BytesIO()
    fig.tight_layout(pad=0.2)
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
