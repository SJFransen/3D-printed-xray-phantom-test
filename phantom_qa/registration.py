"""Phantom detection and image<->phantom-mm registration.

Phantom frame: origin at phantom-face center, +x right / +y up in the canonical
orientation of the guide figure, units mm. Image frame: (x, y) = (col, row), y down.

Pipeline:
  1. coarse: centre-seeded square hypotheses and enclosed-region proposals
  2. rect: jointly score four sides, then refine supported edge profiles
  3. candidates: 8 corner assignments (4 rotations x mirror), similarity fits
  4. scoring: independent structural outlines, not measured QA outcomes
  5. result: best transform + per-candidate scores + verification residuals
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .features import (fit_line_tls, locate_line_center,
                       roi_stats_rect, sample_profile)


# ------------------------------------------------------------------ transform

@dataclass
class Transform:
    A: np.ndarray  # 2x2, mm -> px
    t: np.ndarray  # 2

    def mm_to_px(self, pts):
        pts = np.atleast_2d(np.asarray(pts, float))
        out = pts @ self.A.T + self.t
        return out[0] if out.shape[0] == 1 else out

    def px_to_mm(self, pts):
        pts = np.atleast_2d(np.asarray(pts, float))
        inv = np.linalg.inv(self.A)
        out = (pts - self.t) @ inv.T
        return out[0] if out.shape[0] == 1 else out

    @property
    def px_per_mm(self) -> float:
        return float(np.sqrt(abs(np.linalg.det(self.A))))

    @property
    def mm_per_px(self) -> float:
        return 1.0 / self.px_per_mm

    @property
    def mirrored(self) -> bool:
        # phantom frame is y-up, image frame y-down: a physical (non-mirrored)
        # placement has det > 0 after accounting for the axis flip -> det(A) < 0
        # in raw coordinates means NOT mirrored.
        return bool(np.linalg.det(self.A) > 0)

    @property
    def rotation_deg(self) -> float:
        """In-plane rotation of the phantom's +x axis in the image, degrees."""
        v = self.A @ np.array([1.0, 0.0])
        return float(np.degrees(np.arctan2(v[1], v[0])))

    def to_dict(self):
        return {"A": self.A.tolist(), "t": self.t.tolist()}

    @classmethod
    def from_dict(cls, d):
        return cls(A=np.asarray(d["A"], float), t=np.asarray(d["t"], float))


def fit_affine(src_mm: np.ndarray, dst_px: np.ndarray) -> Transform:
    """Least-squares affine mapping mm -> px from >= 3 correspondences."""
    src = np.asarray(src_mm, float)
    dst = np.asarray(dst_px, float)
    n = len(src)
    M = np.zeros((2 * n, 6))
    b = np.zeros(2 * n)
    M[0::2, 0] = src[:, 0]
    M[0::2, 1] = src[:, 1]
    M[0::2, 4] = 1
    M[1::2, 2] = src[:, 0]
    M[1::2, 3] = src[:, 1]
    M[1::2, 5] = 1
    b[0::2] = dst[:, 0]
    b[1::2] = dst[:, 1]
    p, *_ = np.linalg.lstsq(M, b, rcond=None)
    A = np.array([[p[0], p[1]], [p[2], p[3]]])
    t = np.array([p[4], p[5]])
    return Transform(A=A, t=t)


# ------------------------------------------------------------------ detection

def detect_phantom_mask(pixels: np.ndarray, downscale: int = 8):
    """Largest bright blob in the downsampled image = phantom face.
    Returns (mask, downscale) with mask in downsampled resolution."""
    h, w = pixels.shape
    hh, ww = h // downscale, w // downscale
    small = pixels[:hh * downscale, :ww * downscale].reshape(
        hh, downscale, ww, downscale).mean(axis=(1, 3))
    sm = ndimage.gaussian_filter(small, 2)
    # Otsu threshold
    hist, edges = np.histogram(sm.ravel(), bins=256)
    centers = (edges[:-1] + edges[1:]) / 2
    total = hist.sum()
    best_thr, best_var = centers[0], -1.0
    cum = np.cumsum(hist)
    cum_mean = np.cumsum(hist * centers)
    for i in range(1, 255):
        w0, w1 = cum[i], total - cum[i]
        if w0 == 0 or w1 == 0:
            continue
        m0 = cum_mean[i] / w0
        m1 = (cum_mean[-1] - cum_mean[i]) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var, best_thr = var, centers[i]
    mask = sm > best_thr
    lab, nlab = ndimage.label(mask)
    if nlab == 0:
        raise ValueError("No bright region found — is this a phantom scan?")
    sizes = ndimage.sum(mask, lab, range(1, nlab + 1))
    mask = lab == (1 + int(np.argmax(sizes)))
    mask = ndimage.binary_fill_holes(mask)
    return mask, downscale


def _min_area_angle(points: np.ndarray) -> float:
    """Angle (deg, in [0, 90)) minimizing the axis-aligned bbox area of rotated points."""
    def bbox_area(a):
        r = np.deg2rad(a)
        R = np.array([[np.cos(r), -np.sin(r)], [np.sin(r), np.cos(r)]])
        q = points @ R.T
        return np.ptp(q[:, 0]) * np.ptp(q[:, 1])
    coarse = np.arange(0.0, 90.0, 0.5)
    a0 = coarse[int(np.argmin([bbox_area(a) for a in coarse]))]
    fine = np.arange(a0 - 1.0, a0 + 1.0, 0.02)
    return float(fine[int(np.argmin([bbox_area(a) for a in fine]))]) % 90.0


def detect_phantom_rect(pixels: np.ndarray, downscale: int = 8, initial_corners=None):
    """Detect the phantom's outer square with sub-pixel corners.

    Returns dict with 'corners' (4x2 px, clockwise in image coords), 'edge_lines',
    'edge_points', 'coarse_angle_deg'."""
    from .registration_model import detect
    corners0 = np.asarray(initial_corners if initial_corners is not None
                          else detect(pixels)[0]['corners'], float)
    pts = corners0
    ds = downscale
    direction = corners0[1]-corners0[0]
    angle = float(np.degrees(np.arctan2(direction[1],direction[0])))

    # --- sub-pixel refinement of each edge -------------------------------
    edge_lines = []
    edge_points_all = []
    n_samples = 25
    probe_half = 6.0 * ds  # px on each side of the coarse edge
    for i in range(4):
        c0, c1 = corners0[i], corners0[(i + 1) % 4]
        edge_dir = (c1 - c0) / np.linalg.norm(c1 - c0)
        # outward normal (rect is clockwise in image space -> outward = left of dir)
        normal = np.array([edge_dir[1], -edge_dir[0]])
        center_side = (c0 + c1) / 2
        interior = pts.mean(axis=0)
        if np.dot(center_side - interior, normal) < 0:
            normal = -normal
        pts_edge = []
        for f in np.linspace(0.12, 0.88, n_samples):
            base = c0 + f * (c1 - c0)
            p_in = base - probe_half * normal
            p_out = base + probe_half * normal
            ts, vals = sample_profile(pixels, p_in, p_out, int(2 * probe_half),
                                      avg_half_width=2.0, n_avg=3)
            # Either frame polarity is possible. Refine the nearest supported
            # transition, rather than jumping across a bright surrounding field.
            smooth = ndimage.gaussian_filter1d(vals, 2.)
            derivative = np.abs(np.gradient(smooth))
            prior = np.exp(-.5*((np.arange(len(vals))-len(vals)/2)/(len(vals)/5))**2)
            from .features import centroid_peak
            e = centroid_peak(derivative, int(np.argmax(derivative*prior)), half=2)
            t = np.interp(e, np.arange(len(ts)), ts)
            pts_edge.append(p_in + t * (p_out - p_in) / np.linalg.norm(p_out - p_in))
        pts_edge = np.asarray(pts_edge)
        # robust line fit: 2 rounds of outlier rejection
        keep = np.ones(len(pts_edge), bool)
        for _ in range(2):
            c, u = fit_line_tls(pts_edge[keep])
            nvec = np.array([-u[1], u[0]])
            d = np.abs((pts_edge - c) @ nvec)
            mad = np.median(d[keep]) + 1e-9
            keep = d < max(5 * mad, 2.0)
        c, u = fit_line_tls(pts_edge[keep])
        edge_lines.append((c, u))
        edge_points_all.append(pts_edge[keep])

    # --- corners = intersections of adjacent edge lines ------------------
    corners = []
    for i in range(4):
        c1_, u1 = edge_lines[(i - 1) % 4]
        c2_, u2 = edge_lines[i]
        M = np.array([u1, -u2]).T
        rhs = c2_ - c1_
        st = np.linalg.solve(M, rhs)
        corners.append(c1_ + st[0] * u1)
    corners = np.asarray(corners)

    return {"corners": corners, "edge_lines": edge_lines,
            "edge_points": edge_points_all, "coarse_angle_deg": angle,
            "mask_downscale": ds}


# ------------------------------------------------------------------ orientation

def candidate_transforms(corners_px: np.ndarray, side_mm: float):
    """8 candidate affines (4 cyclic corner assignments x optional mirror)."""
    L = side_mm / 2.0
    nominal = np.array([[-L, L], [L, L], [L, -L], [-L, -L]])  # TL TR BR BL (y up)
    cands = []
    seqs = [np.roll(np.arange(4), -k) for k in range(4)]
    seqs += [np.roll(np.arange(4)[::-1], -k) for k in range(4)]
    for seq in seqs:
        T = fit_affine(nominal, corners_px[seq])
        cands.append((T, {"assignment": seq.tolist()}))
    return cands


def score_transform(pixels: np.ndarray, T: Transform, pdef) -> dict:
    """Score how well transform T matches the phantom layout, using
    orientation-discriminating objects from the phantom definition."""
    score = 0.0
    parts = {}

    # 1. wedge: sampled means must decrease from the top step to the bottom step
    # (positional expectation only — no material assumption)
    w = pdef.wedge
    n_steps = len(w["steps"])
    step_h = (w["y_top_mm"] - w["y_bottom_mm"]) / n_steps
    means = []
    for i in range(n_steps):
        yc = w["y_top_mm"] - (i + 0.5) * step_h
        c = T.mm_to_px([w["center_x_mm"], yc])
        s = roi_stats_rect(pixels, c, (T.px_per_mm * 6, T.px_per_mm * 6))
        means.append(s["mean"])
    means = np.asarray(means)
    expected = -np.arange(n_steps, dtype=float)      # top brightest
    denom = means.std() * expected.std()
    corr = float(np.mean((means - means.mean()) * (expected - expected.mean()))
                 / denom) if denom > 0 else 0.0
    rng = float(np.ptp(means)) / (np.ptp(pixels) + 1e-9)
    parts["wedge_corr"] = corr
    parts["wedge_range"] = rng
    score += 3.0 * corr + 2.0 * min(rng, 1.0)

    # 2. line-pair groups: high local std
    stds = []
    for g in pdef.linepairs["groups"]:
        c = T.mm_to_px(g["center_mm"])
        s = roi_stats_rect(pixels, c, (T.px_per_mm * 8, T.px_per_mm * 8))
        stds.append(s["std"])
    med_bg = float(np.median(pixels))
    parts["linepair_std"] = float(np.median(stds))
    score += 2.0 * min(parts["linepair_std"] / (0.02 * med_bg + 1e-9), 1.0)

    # 3. low-contrast block: bright and smooth
    c = T.mm_to_px(pdef.lowcontrast["center_mm"])
    s = roi_stats_rect(pixels, c, (T.px_per_mm * 10, T.px_per_mm * 10))
    parts["lc_mean_rel"] = s["mean"] / (med_bg + 1e-9)
    score += min(max(parts["lc_mean_rel"] - 1.0, 0.0) * 10.0, 1.0)

    parts["total"] = float(score)
    return parts


@dataclass
class Registration:
    transform: Transform
    corners_px: np.ndarray
    coarse_angle_deg: float
    score: dict
    candidate_scores: list = field(default_factory=list)
    landmarks: dict = field(default_factory=dict)
    residual_rms_mm: float = float("nan")

    def summary(self) -> dict:
        return {
            "px_per_mm": self.transform.px_per_mm,
            "mm_per_px": self.transform.mm_per_px,
            "rotation_deg": self.transform.rotation_deg,
            "mirrored": self.transform.mirrored,
            "residual_rms_mm": self.residual_rms_mm,
            "score": self.score,
            "corners_px": self.corners_px.tolist(),
        }


def verify_landmarks(pixels: np.ndarray, T: Transform, pdef) -> tuple[dict, float]:
    """Detect tape-measure central long lines near their predicted positions and
    report prediction residuals (mm). These verify — they do not drive — the fit."""
    landmarks = {}
    resid = []
    ppm = T.px_per_mm
    for side, ruler in pdef.rulers.items():
        pred_px = np.asarray(T.mm_to_px(ruler["center_line_mid_mm"]), float)
        # ruler lines are PARALLEL to the edge; their position varies along the
        # inward normal, so that is the search direction
        normal_mm = np.asarray(ruler["normal_inward_mm"], float)
        d_px = T.mm_to_px(normal_mm) - T.mm_to_px([0, 0])
        d_px = d_px / np.linalg.norm(d_px)
        found, strength = locate_line_center(
            pixels, pred_px, d_px, search_half=2.2 * ppm,
            avg_half_width=3.0 * ppm, n_avg=7)
        if found is not None:
            # error only along the normal (position along the line is unconstrained)
            delta_mm = np.asarray(T.px_to_mm(found)) - np.asarray(
                ruler["center_line_mid_mm"])
            err_mm = float(abs(np.dot(delta_mm, normal_mm)))
            landmarks[side] = {"predicted_px": pred_px.tolist(),
                               "detected_px": list(found),
                               "err_mm": err_mm, "strength": strength}
            resid.append(err_mm)
        else:
            landmarks[side] = {"predicted_px": pred_px.tolist(),
                               "detected_px": None, "err_mm": None,
                               "strength": 0.0}
    rms = float(np.sqrt(np.mean(np.square(resid)))) if resid else float("nan")
    return landmarks, rms


def register(pixels: np.ndarray, pdef, corners_hint: np.ndarray | None = None,
             spacing_mm: float | None = None):
    """Full registration. ``corners_hint`` (4x2 px) overrides automatic rect
    detection (manual corner clicks from the UI)."""
    if corners_hint is not None:
        rect = {"corners": np.asarray(corners_hint, float),
                "coarse_angle_deg": float("nan")}
        rectangles = [rect]
    else:
        from .registration_model import detect
        prior = pdef.side_mm / spacing_mm if spacing_mm is not None and spacing_mm > 0 else None
        rectangles = []
        for candidate in detect(pixels, side_px_prior=prior)[:4]:
            try:
                rectangles.append(detect_phantom_rect(pixels, initial_corners=candidate['corners']))
            except (ValueError, np.linalg.LinAlgError):
                continue
        if not rectangles:
            raise ValueError('Phantom borders could not be refined; place the corners manually.')

    scored = []
    from .registration_model import structural_score
    for rect in rectangles:
        for T, meta in candidate_transforms(rect['corners'], pdef.side_mm):
            if corners_hint is None:
                # Closest uniform-scale orthogonal transform; retain reflection.
                u, s, vt = np.linalg.svd(T.A)
                T = Transform((u @ vt)*np.mean(s), T.t)
            parts = structural_score(pixels, T, pdef)
            scored.append((parts['total'], T, {**meta, 'rect': rect}, parts))
    scored.sort(key=lambda x: -x[0])
    best_score, best_T, best_meta, best_parts = scored[0]

    alternatives = [score for score,T,_,_ in scored[1:] if
                    np.linalg.norm(T.A/T.px_per_mm-best_T.A/best_T.px_per_mm) > .25]
    margin = best_score-max(alternatives) if alternatives else best_score

    if corners_hint is None and (best_score < .10 or margin < .025 or
            sum(v > .01 for k,v in best_parts.items() if k != 'total') < 2):
        raise ValueError('Phantom layout could not be verified; place the corners manually.')
    best_parts = {**best_parts, 'orientation_margin': float(margin),
                  'method': 'square_structural_model' if corners_hint is None else 'manual_corners'}

    landmarks, rms = verify_landmarks(pixels, best_T, pdef)

    return Registration(
        transform=best_T,
        corners_px=best_meta['rect']["corners"],
        coarse_angle_deg=best_meta['rect'].get("coarse_angle_deg", float("nan")),
        score=best_parts,
        candidate_scores=[{"total": s, "assignment": m["assignment"]}
                          for s, _, m, _ in scored],
        landmarks=landmarks,
        residual_rms_mm=rms,
    )
