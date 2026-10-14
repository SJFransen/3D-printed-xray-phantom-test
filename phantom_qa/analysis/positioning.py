"""Template-first, bounded coarse-to-fine positioning in phantom millimetres.

Only pose (translation and rotation) is fitted. Sizes and spacings are never
optimisation variables: those are quantities the QA tests must still measure.
Templates are sampled masks rather than full-detector raster allocations.
"""

from __future__ import annotations

import itertools

import numpy as np
from scipy import ndimage


STEPS_MM = (2.0, 1.0, 0.5, 0.25)
STEPS_DEG = (2.0, 1.0, 0.5, 0.25)


def _finite_score(value):
    return float(value) if np.isfinite(value) else None


def rotation(angle_deg):
    a = np.deg2rad(angle_deg)
    return np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])


def project(points, center, angle_deg):
    return np.asarray(points) @ rotation(angle_deg).T + np.asarray(center)


def build_templates(ctx):
    """Project the complete fixed layout through the scan's registration.

    Each entry supplies a starting pose and a local search mask. Geometry field
    edges are exposure-dependent and deliberately have no phantom template.
    """
    p = ctx.pdef
    out = {}

    def add(key, center, size, angle=0.0, search=8.0, turn=8.0):
        half = np.asarray(size, float) / 2
        corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * half
        # A square search mask conservatively encloses all permitted rotations.
        radius = float(np.linalg.norm(half)) + search
        search_corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * radius
        out[key] = {
            "center_mm": list(center), "size_mm": list(size),
            "angle_deg": float(angle), "search_mm": float(search),
            "angle_limit_deg": float(turn),
            "center_px": np.asarray(ctx.T.mm_to_px(center)).tolist(),
            "corners_px": ctx.T.mm_to_px(project(corners, center, angle)).tolist(),
            "search_mask_px": ctx.T.mm_to_px(search_corners + center).tolist(),
        }

    def uniformity():
        for sq in p.uniformity["squares"]:
            add("uniformity/" + sq["id"], sq["center_mm"],
                [p.uniformity["size_mm"]] * 2,
                search=p.uniformity.get("search_mm", 8.0),
                turn=p.uniformity.get("angle_limit_deg", 8.0))

    def lowcontrast():
        lc = p.lowcontrast
        add("lowcontrast/block", lc["center_mm"], lc["size_mm"], lc["angle_deg"],
            lc.get("search_mm", 15.0), lc.get("angle_limit_deg", 25.0))

    def linepairs():
        lp = p.linepairs
        strip_angle = lp.get("strip_angle_deg", 45.0)
        block_size = lp.get("roi_size_mm", 12.6)
        centers = np.array([g["center_mm"] for g in lp["groups"]])
        center = centers.mean(axis=0)
        local = (centers - center) @ rotation(strip_angle)
        size = np.ptp(local, axis=0) + block_size
        add("linepairs/strip", center, size, strip_angle,
            lp.get("search_pad_mm", 28.0), lp.get("angle_limit_deg", 15.0))
        for g in lp["groups"]:
            add("linepairs/" + g["id"], g["center_mm"], [block_size] * 2,
                strip_angle, search=5.0, turn=0.0)

    def wedge():
        w = p.wedge
        add("wedge/frame", [w["center_x_mm"], (w["y_top_mm"] + w["y_bottom_mm"]) / 2],
            [w.get("frame_width_mm", 20.0), w["y_top_mm"] - w["y_bottom_mm"]],
            search=w.get("search_mm", 12.0), turn=w.get("angle_limit_deg", 8.0))

    def rulers():
        for side, ruler in p.rulers.items():
            add("ruler/" + side, ruler["center_line_mid_mm"], [30.0, 30.0],
                search=2.2, turn=0.0)

    for build in (uniformity, lowcontrast, linepairs, wedge, rulers):
        try:
            build()
        except (KeyError, TypeError, ValueError):
            # Preserve propose_all's per-test failure isolation. The affected
            # module reports its definition/template error when it proposes;
            # the remaining feature masks are still available.
            continue
    return out


def template_for(ctx, key):
    if ctx.templates is None:
        ctx.templates = build_templates(ctx)
    return ctx.templates[key]


def refine_pose(score, center_mm, angle_deg=0.0, *, search_mm=8.0,
                angle_limit_deg=8.0, max_iterations=12):
    """Maximise a structural score at successively smaller pose steps.

    ``score(center, angle, level)`` can smooth its evidence differently at each
    level. A coarse grid covers the entire bounded search before local ascent,
    preventing the first weak local maximum from deciding the result.
    """
    origin = np.asarray(center_mm, float)
    pose = np.array([*origin, angle_deg], float)
    initial = float(score(pose[:2], pose[2], 0))
    evaluations = 1
    iterations = 0

    def evaluate(candidate, level):
        nonlocal evaluations
        if (np.linalg.norm(candidate[:2] - origin) > search_mm + 1e-8
                or abs(candidate[2] - angle_deg) > angle_limit_deg + 1e-8):
            return -np.inf
        evaluations += 1
        value = float(score(candidate[:2], candidate[2], level))
        return value if np.isfinite(value) else -np.inf

    def offsets(bound, stride):
        return np.unique(np.r_[np.arange(-bound, bound + 1e-8, stride), 0.0, bound])

    best = initial if np.isfinite(initial) else -np.inf
    for dx, dy, da in itertools.product(offsets(search_mm, 4.0),
                                        offsets(search_mm, 4.0),
                                        offsets(angle_limit_deg, 4.0)):
        candidate = np.array([origin[0] + dx, origin[1] + dy, angle_deg + da])
        value = evaluate(candidate, 0)
        if value > best + 1e-9:
            pose, best = candidate, value

    levels = []
    for level, (step, turn) in enumerate(zip(STEPS_MM, STEPS_DEG)):
        best = evaluate(pose, level)
        for _ in range(max_iterations):
            iterations += 1
            next_pose, next_score = pose, best
            turns = (-turn, 0.0, turn) if angle_limit_deg else (0.0,)
            for delta in itertools.product((-step, 0.0, step),
                                           (-step, 0.0, step), turns):
                candidate = pose + delta
                value = evaluate(candidate, level)
                if value > next_score + 1e-9:
                    next_pose, next_score = candidate, value
            if next_score <= best + 1e-9:
                break
            pose, best = next_pose, next_score
        levels.append({"step_mm": step, "step_deg": turn, "score": _finite_score(best)})

    # Compare improvement using the SAME fine-scale score, not across blurs.
    initial_fine = float(score(origin, angle_deg, len(STEPS_MM) - 1))
    shift = float(np.linalg.norm(pose[:2] - origin))
    at_bound = (search_mm > 0 and shift >= search_mm - STEPS_MM[-1] / 2)
    at_bound |= (angle_limit_deg > 0 and
                 abs(pose[2] - angle_deg) >= angle_limit_deg - STEPS_DEG[-1] / 2)
    return {"method": "coarse_to_fine", "initial_center_mm": origin.tolist(),
            "initial_angle_deg": float(angle_deg), "center_mm": pose[:2].tolist(),
            "angle_deg": float(pose[2]), "initial_score": _finite_score(initial_fine),
            "score": _finite_score(best), "shift_mm": shift, "at_bound": bool(at_bound),
            "iterations": iterations, "evaluations": evaluations, "levels": levels}


def rectangle_outline(size_mm, samples=21):
    """Side midsections avoid rounded corners and corner intersections."""
    hx, hy = np.asarray(size_mm) / 2
    t = np.linspace(-0.7, 0.7, samples)
    points = np.vstack([np.c_[np.full(samples, -hx), t * hy],
                        np.c_[np.full(samples, hx), t * hy],
                        np.c_[t * hx, np.full(samples, -hy)],
                        np.c_[t * hx, np.full(samples, hy)]])
    normals = np.repeat([[-1, 0], [1, 0], [0, -1], [0, 1]], samples, axis=0)
    return points, normals, np.repeat(np.arange(4), samples)


class OutlineMatcher:
    """Match structural lines/edges on a small rectified image pyramid.

    The score is a noise-normalised mean response per outline section. Every
    section must have image coverage; missing samples never use nearest-pixel
    extrapolation. QA ROI means, SD and CNR are not scoring inputs.
    """

    def __init__(self, ctx, template, points, normals, sections, mode="line"):
        self.points = np.asarray(points, float)
        self.normals = np.asarray(normals, float)
        self.sections = np.asarray(sections)
        self.mode = mode
        self.labels = np.unique(self.sections)
        self.step = max(0.25, ctx.T.mm_per_px)
        radius = float(np.max(np.linalg.norm(self.points, axis=1)))
        radius += template["search_mm"] + 4.0
        cx, cy = template["center_mm"]
        self.x0, self.y0 = cx - radius, cy + radius
        xs = np.arange(cx - radius, cx + radius, self.step)
        ys = np.arange(cy + radius, cy - radius, -self.step)
        xx, yy = np.meshgrid(xs, ys)
        px = ctx.T.mm_to_px(np.c_[xx.ravel(), yy.ravel()])
        h, w = ctx.pixels.shape
        valid = ((px[:, 0] >= 0) & (px[:, 0] <= w - 1)
                 & (px[:, 1] >= 0) & (px[:, 1] <= h - 1))
        self.valid = valid.reshape(xx.shape).astype(float)
        image = ndimage.map_coordinates(ctx.pixels, [px[:, 1], px[:, 0]],
                                        order=1, mode="nearest").reshape(xx.shape)
        self.images = [ndimage.gaussian_filter(image, blur / self.step)
                       for blur in (1.0, 0.7, 0.45, 0.25)]
        values = image[self.valid > 0]
        differences = np.diff(image, axis=1)[(self.valid[:, 1:] * self.valid[:, :-1]) > 0]
        noise = float(np.median(np.abs(differences))) / 0.954 if differences.size else 1.0
        self.noise = max(noise, float(np.ptp(values)) * 1e-6, 1e-6) if values.size else 1.0

    def sample(self, image, points):
        coords = [(self.y0 - points[:, 1]) / self.step,
                  (points[:, 0] - self.x0) / self.step]
        return ndimage.map_coordinates(image, coords, order=1, mode="constant", cval=0.0)

    def responses(self, center, angle, level):
        points = project(self.points, center, angle)
        normals = self.normals @ rotation(angle).T
        inside, outside = points - normals * 1.25, points + normals * 1.25
        coverage = np.minimum(self.sample(self.valid, inside), self.sample(self.valid, outside))
        image = self.images[level]
        a, b = self.sample(image, inside), self.sample(image, outside)
        if self.mode == "line":
            response = self.sample(image, points) - (a + b) / 2
        elif self.mode == "dark_edge":
            response = b - a
        else:  # boundary polarity can vary between wedge steps
            response = np.abs(a - b)
        return np.array([float(np.mean(response[self.sections == label])) / self.noise
                         if np.all(coverage[self.sections == label] > 0.99) else -np.inf
                         for label in self.labels])

    def score(self, center, angle, level):
        response = self.responses(center, angle, level)
        # Balance outline sections so one high-contrast edge cannot pull a
        # whole group away from several weaker, correctly placed edges.
        return float(np.mean(np.log1p(np.maximum(response, 0)))) if np.isfinite(response).all() else -np.inf

    def fit(self, template, required_sections=3, min_section_score=2.0):
        result = refine_pose(self.score, template["center_mm"], template["angle_deg"],
                             search_mm=template["search_mm"],
                             angle_limit_deg=template["angle_limit_deg"])
        response = self.responses(result["center_mm"], result["angle_deg"], 3)
        accepted = (np.isfinite(response).all() and
                    np.count_nonzero(response > min_section_score) >= required_sections and
                    result["score"] is not None and
                    (result["initial_score"] is None or
                     result["score"] >= result["initial_score"] - 1e-9) and
                    not result["at_bound"])
        result["accepted"] = bool(accepted)
        result["section_scores"] = [float(x) if np.isfinite(x) else None for x in response]
        result["reason"] = ("structural match" if accepted else
                            "weak, clipped or bounded structural match; template retained")
        if not accepted:
            result["candidate_center_mm"] = result["center_mm"]
            result["candidate_angle_deg"] = result["angle_deg"]
            result["candidate_shift_mm"] = result["shift_mm"]
            result["candidate_score"] = result["score"]
            result["center_mm"] = list(template["center_mm"])
            result["angle_deg"] = template["angle_deg"]
            result["shift_mm"] = 0.0
            result["score"] = result["initial_score"]
        return result
