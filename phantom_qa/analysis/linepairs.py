"""Spatial-resolution line-pair test: SD constancy + line-pattern linearity.

Guide metric: standard deviation inside a consistently placed ROI per group.
Additional first-class output (rev. 2 workplan): intensity profile across each
group, detected line positions vs an ideal uniform grid, pitch deviation.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from ..features import (centroid_peak, find_peaks_1d, locate_line_center, sample_profile)
from .common import Ctx, rect_roi, segment, stats_for_roi
from .positioning import STEPS_MM, project, refine_pose, rotation, template_for


_RECT_STEP_MM = 0.25          # sampling of the local rectified corridor
_PERIOD_WIN_MM = 10.0         # analysis window for the periodicity map
_PERIOD_STRIDE_MM = 2.0
_FREQ_LO, _FREQ_HI = 1.0, 2.4  # cycles/mm band that contains all line groups


def _rectify_region(ctx: Ctx, x_range, y_range, step_mm=_RECT_STEP_MM):
    """Resample an axis-aligned region of the phantom frame onto a mm grid.
    Returns (image, x0_mm, y0_mm, step) with row 0 = y_range[1] (top)."""
    xs = np.arange(x_range[0], x_range[1], step_mm)
    ys = np.arange(y_range[1], y_range[0], -step_mm)
    XX, YY = np.meshgrid(xs, ys)
    pts = np.stack([XX.ravel(), YY.ravel()], axis=1)
    px = pts @ ctx.T.A.T + ctx.T.t
    img = ndimage.map_coordinates(
        ctx.pixels, [px[:, 1].reshape(XX.shape), px[:, 0].reshape(XX.shape)],
        order=1, mode="constant", cval=0.0)
    return img, xs[0], ys[0], step_mm


def _periodicity_map(img, step_mm, win_mm=_PERIOD_WIN_MM,
                     stride_mm=_PERIOD_STRIDE_MM):
    """Band-limited periodicity: peak/median energy ratio in the line-pair band.

    Local VARIANCE cannot be used to find the line groups — the dark wedge steps
    carry far more quantum noise than the tungsten patterns carry signal, so a
    variance detector locks onto the wedge. Noise is broadband, a printed grating
    is a sharp spectral peak, so the peak-to-median ratio separates them cleanly.

    Returns (ratio_map, freq_map, dir_map, win_px, stride_px)."""
    w = int(round(win_mm / step_mm))
    st = max(int(round(stride_mm / step_mm)), 1)
    f = np.fft.fftshift(np.fft.fftfreq(w, d=step_mm))
    FX, FY = np.meshgrid(f, f)
    fmag = np.hypot(FX, FY)
    band = (fmag > _FREQ_LO) & (fmag < _FREQ_HI)
    han = np.outer(np.hanning(w), np.hanning(w))
    rows = range(0, img.shape[0] - w, st)
    cols = range(0, img.shape[1] - w, st)
    nr, nc = len(list(rows)), len(list(cols))
    ratio = np.zeros((nr, nc))
    freq = np.zeros((nr, nc))
    ang = np.zeros((nr, nc))
    for i, r in enumerate(range(0, img.shape[0] - w, st)):
        for j, c in enumerate(range(0, img.shape[1] - w, st)):
            patch = img[r:r + w, c:c + w]
            if patch.min() <= 0:          # outside the detector / phantom
                continue
            F = np.abs(np.fft.fftshift(np.fft.fft2((patch - patch.mean()) * han)))
            bv = F[band]
            if bv.size == 0:
                continue
            pk = float(bv.max())
            med = float(np.median(bv)) + 1e-9
            ratio[i, j] = pk / med
            iy, ix = np.unravel_index(int(np.argmax(np.where(band, F, 0))), F.shape)
            freq[i, j] = fmag[iy, ix]
            # Row index runs along DECREASING phantom y, so the row-axis
            # frequency is the negative of the phantom-y frequency. Without the
            # sign flip the angle comes out mirrored about the x axis.
            ang[i, j] = np.degrees(np.arctan2(-FY[iy, ix], FX[iy, ix])) % 180.0
    return ratio, freq, ang, w, st


def _refine_center_fine(img, x0, y0, step, center_mm, freq_lp_mm, angle_deg,
                        block_mm, search_mm=5.0):
    """Sub-window refinement of a block centre.

    The strip template supplies the coarse centre. Demodulation at the block's
    measured frequency and modulation direction produces a pattern envelope;
    its iterated centre of mass supplies the local correction."""
    # A directional narrow-band envelope suppresses the broadband printed
    # frame. An isotropic bandpass can give that frame more energy than a weak
    # grating and consequently pull the centre onto the outline.
    pitch = 1.0 / max(freq_lp_mm, 1e-6)
    yy, xx = np.indices(img.shape)
    a = np.deg2rad(angle_deg)
    phase = 2 * np.pi * freq_lp_mm * (xx * step * np.cos(a) - yy * step * np.sin(a))
    detrended = img - ndimage.gaussian_filter(img, max(2 * pitch / step, 1.0))
    carrier = detrended * np.exp(-1j * phase)
    sigma = max(1.5 * pitch / step, 1.0)
    envelope = (ndimage.gaussian_filter(carrier.real, sigma)
                + 1j * ndimage.gaussian_filter(carrier.imag, sigma))
    energy = np.abs(envelope) ** 2
    energy = np.maximum(energy - ndimage.gaussian_filter(energy, block_mm / step), 0.0)

    # Isotropic, iterated centre of mass. A box/uniform filter would bias the
    # result for a block rotated ~45 deg to the sampling grid; a disc does not.
    rad = block_mm / 2.0
    cur = [float(center_mm[0]), float(center_mm[1])]
    h, w = energy.shape
    # Large centre-of-mass corrections first, then progressively smaller
    # bounded corrections. This refines pattern support, never the QA SD.
    for max_move in np.repeat(STEPS_MM, 4):
        cj = (cur[0] - x0) / step
        ci = (y0 - cur[1]) / step
        r = int(round(rad / step))
        i0, i1 = int(max(ci - r, 0)), int(min(ci + r + 1, h))
        j0, j1 = int(max(cj - r, 0)), int(min(cj + r + 1, w))
        if i1 - i0 < 3 or j1 - j0 < 3:
            return cur
        sub = energy[i0:i1, j0:j1]
        ii, jj = np.mgrid[i0:i1, j0:j1]
        disc = ((ii - ci) ** 2 + (jj - cj) ** 2) <= (rad / step) ** 2
        wv = np.where(disc, sub, 0.0)
        tot = wv.sum()
        if tot <= 0:
            return cur
        ci_f = float((ii * wv).sum() / tot)
        cj_f = float((jj * wv).sum() / tot)
        new = [float(x0 + cj_f * step), float(y0 - ci_f * step)]
        moved = np.hypot(new[0] - cur[0], new[1] - cur[1])
        if moved > max_move:
            new = (np.asarray(cur) + (np.asarray(new) - cur) * max_move / moved).tolist()
        # never wander outside the coarse search radius
        if np.hypot(new[0] - center_mm[0], new[1] - center_mm[1]) > search_mm:
            return cur
        cur = new
        if moved < 0.05:
            break
    return cur


def _axis_through(blocks):
    """Principal axis (deg, mod 180) through a set of block centres."""
    if len(blocks) < 2:
        return None
    P = np.array([b["center_mm"] for b in blocks], float)
    C = P - P.mean(axis=0)
    if not np.isfinite(C).all() or np.allclose(C, 0):
        return None
    _, _, vt = np.linalg.svd(C, full_matrices=False)
    v = vt[0]
    return float(np.degrees(np.arctan2(v[1], v[0])) % 180.0)


def _align_blocks_to_middle_line(ctx: Ctx, blocks, strip_angle):
    """Final transverse alignment to the printed connector between gratings.

    Probe only the gaps, where the connector is not obscured by the pattern.
    The small search tube excludes the two outer frame lines. A robust straight
    fit sets the strip angle; projection preserves every block's position along
    that line. Without sufficient evidence the existing placement is retained.
    """
    if len(blocks) < 3:
        return blocks, strip_angle
    points = np.array([b["center_mm"] for b in blocks], float)
    origin = points.mean(axis=0)
    u = rotation(strip_angle)[:, 0]
    v = np.array([-u[1], u[0]])
    along = np.sort((points - origin) @ u)
    nominal = np.array([g["center_mm"] for g in ctx.pdef.linepairs["groups"]])
    pitch = float(np.median(np.diff(np.sort(nominal @ u))))
    block_mm = ctx.pdef.linepairs.get("roi_size_mm", 12.6)
    search_mm = 4.0
    samples, gaps = [], []
    direction_px = ctx.T.A @ v
    direction_px /= np.linalg.norm(direction_px)
    h, w = ctx.pixels.shape
    for gap, (left, right) in enumerate(zip(along[:-1], along[1:])):
        # Skip gaps spanning a missing group and keep away from grating edges.
        if not block_mm * np.sqrt(2) + 3 < right - left < 1.5 * pitch:
            continue
        for offset in (-1.0, 0.0, 1.0):
            probe = origin + ((left + right) / 2 + offset) * u
            bounds = np.array([probe + a * search_mm * v + b * 0.75 * u
                               for a in (-1, 1) for b in (-1, 1)])
            bounds_px = ctx.T.mm_to_px(bounds)
            if (np.any(bounds_px < 0) or np.any(bounds_px[:, 0] > w - 1)
                    or np.any(bounds_px[:, 1] > h - 1)):
                continue
            found, _ = locate_line_center(
                ctx.pixels, ctx.T.mm_to_px(probe), direction_px,
                search_half=search_mm * np.linalg.norm(ctx.T.A @ v), n=81,
                avg_half_width=0.75 * np.linalg.norm(ctx.T.A @ u), n_avg=5)
            if found is not None:
                delta = ctx.T.px_to_mm(found) - origin
                transverse = float(delta @ v)
                if abs(transverse) < search_mm - 0.25:
                    samples.append([float(delta @ u), transverse])
                    gaps.append(gap)
    if len(samples) < 6 or len(set(gaps)) < 2:
        return blocks, strip_angle
    samples = np.array(samples)
    A = np.c_[samples[:, 0], np.ones(len(samples))]
    keep = np.ones(len(samples), dtype=bool)
    for _ in range(3):
        if keep.sum() < 6 or len(set(np.asarray(gaps)[keep])) < 2:
            return blocks, strip_angle
        coef = np.linalg.lstsq(A[keep], samples[keep, 1], rcond=None)[0]
        residual = samples[:, 1] - A @ coef
        med = float(np.median(residual[keep]))
        mad = float(np.median(np.abs(residual[keep] - med)))
        keep = np.abs(residual - med) <= max(0.25, 4.0 * mad)
    if keep.sum() < 6 or len(set(np.asarray(gaps)[keep])) < 2:
        return blocks, strip_angle
    slope, intercept = np.linalg.lstsq(A[keep], samples[keep, 1], rcond=None)[0]
    rms = float(np.sqrt(np.mean((samples[keep, 1] - A[keep] @ [slope, intercept]) ** 2)))
    if rms > 0.3 or abs(np.degrees(np.arctan(slope))) > 5.0:
        return blocks, strip_angle
    normal = (v - slope * u) / np.hypot(1, slope)
    line_origin = origin + intercept * v
    shifts = (points - line_origin) @ normal
    if np.max(np.abs(shifts)) > search_mm:
        return blocks, strip_angle
    aligned = points - shifts[:, None] * normal
    angle = float((strip_angle + np.degrees(np.arctan(slope))) % 180)
    for block, center, shift in zip(blocks, aligned, shifts):
        block["positioning"]["middle_line"] = {
            "initial_center_mm": list(block["center_mm"]),
            "center_mm": center.tolist(), "shift_mm": float(abs(shift)),
            "angle_deg": angle, "fit_rms_mm": rms, "n_samples": int(keep.sum()),
        }
        block["center_mm"] = center.tolist()
        block["t"] = float(center @ rotation(angle)[:, 0])
    return blocks, angle


def detect_line_blocks(ctx: Ctx, ratio_threshold: float = 12.0):
    """Align the strip template, then refine each block in its own local mask.

    Whole-strip connected components can merge neighbouring groups through the
    printed frame. Here a shared rigid pose preserves their approximate spacing
    and gives every group a distinct, small search region before fine centring.
    """
    lp = ctx.pdef.linepairs
    template = template_for(ctx, "linepairs/strip")
    nominal = np.array([g["center_mm"] for g in lp["groups"]], float)
    pad = template["search_mm"] + lp["roi_size_mm"]
    img, x0, y0, step = _rectify_region(
        ctx, (nominal[:, 0].min() - pad, nominal[:, 0].max() + pad),
        (nominal[:, 1].min() - pad, nominal[:, 1].max() + pad))
    ratio, freq, ang, w, st = _periodicity_map(img, step)
    if not ratio.size or np.max(ratio) <= ratio_threshold:
        return [], None

    # Axis-aligned isolated frame/ruler lines are not diagonal gratings. The
    # orientation allowance follows the strip template's permitted rotation.
    base_angle = template["angle_deg"]
    d = np.abs((ang - base_angle + 90) % 180 - 90)
    orientation_ok = np.minimum(d, 90 - d) <= template["angle_limit_deg"] + 12
    response = np.log1p(np.maximum(ratio - ratio_threshold, 0)) * orientation_ok
    maps = [ndimage.gaussian_filter(response, sigma) for sigma in (0.8, 0.5, 0.25, 0.0)]

    def sample(array, centers):
        centers = np.atleast_2d(centers)
        coords = [((y0 - centers[:, 1]) / step - w / 2) / st,
                  ((centers[:, 0] - x0) / step - w / 2) / st]
        return ndimage.map_coordinates(array, coords, order=1, mode="constant", cval=0.0)

    local = (nominal - template["center_mm"]) @ rotation(base_angle)

    def group_score(center, angle, level):
        values = sample(maps[level], project(local, center, angle))
        return float(np.mean(values))

    positioning = refine_pose(group_score, template["center_mm"], base_angle,
                              search_mm=template["search_mm"],
                              angle_limit_deg=template["angle_limit_deg"])
    predicted = project(local, positioning["center_mm"], positioning["angle_deg"])
    support = sample(response, predicted)
    positioning["accepted"] = bool(np.count_nonzero(support > 0.5) >= min(3, len(nominal))
                                    and not positioning["at_bound"])
    if not positioning["accepted"]:
        return [], None

    blocks = []
    block_mm = lp.get("roi_size_mm", 12.6)
    strip_angle = positioning["angle_deg"] % 180.0
    u = rotation(strip_angle)[:, 0]
    for expected in predicted:
        center = expected.tolist()
        if sample(response, center)[0] <= 0.5:
            continue
        measured = _measure_pattern_2dfft(ctx, center)
        if measured is None:
            continue
        frequency, direction_px = measured
        if not _FREQ_LO - 0.15 <= frequency <= _FREQ_HI:
            continue
        direction = np.linalg.solve(ctx.T.A, direction_px)
        mod_dir = float(np.degrees(np.arctan2(direction[1], direction[0])) % 180)
        refined = _refine_center_fine(img, x0, y0, step, center, frequency,
                                      mod_dir, block_mm)
        fine = {"method": "directional_pattern_centroid", "steps_mm": list(STEPS_MM),
                "initial_center_mm": center, "center_mm": refined,
                "shift_mm": float(np.linalg.norm(np.asarray(refined) - center))}
        blocks.append({"center_mm": refined, "coarse_center_mm": list(center),
                       "template_center_mm": expected.tolist(),
                       "freq_lp_mm": float(frequency), "mod_dir_deg": mod_dir,
                       "n_windows": int(np.count_nonzero(
                           sample(response, np.asarray(center) + np.array(
                               [[dx, dy] for dx in (-2, 0, 2) for dy in (-2, 0, 2)])) > 0)),
                       "t": float(np.dot(refined, u)),
                       "positioning": {"strip": positioning, "local": fine}})
    blocks.sort(key=lambda b: b["t"])
    measured_axis = _axis_through(blocks)
    final_angle = measured_axis if measured_axis is not None else strip_angle
    return _align_blocks_to_middle_line(ctx, blocks, final_angle)


def _match_blocks_to_groups(ctx: Ctx, blocks, strip_angle):
    """Match frequency AND spatial order, including incomplete/reversed strips.

    A greedy frequency match can give 1.2 lp/mm the measured 1.1 block, leaving
    1.1 to take 1.2. Enumerating the small ordered subsets keeps identities
    consistent when a group is missing, without inventing a detected block.
    """
    from itertools import combinations

    if not blocks:
        return {}
    groups = ctx.pdef.linepairs["groups"]
    u = rotation(strip_angle)[:, 0]
    order = np.argsort([np.dot(g["center_mm"], u) for g in groups])
    blocks = sorted(blocks, key=lambda b: np.dot(b["center_mm"], u))
    for count in range(min(len(groups), len(blocks)), 0, -1):
        best, best_cost = {}, np.inf
        for sequence in (order, order[::-1]):
            for selected_groups in combinations(sequence, count):
                for selected_blocks in combinations(range(len(blocks)), count):
                    errors = np.array([abs(groups[gi]["freq_lp_mm"] - blocks[bi]["freq_lp_mm"])
                                       for gi, bi in zip(selected_groups, selected_blocks)])
                    if np.any(errors > 0.35):
                        continue
                    cost = float(np.sum(errors ** 2))
                    if cost < best_cost:
                        best_cost = cost
                        best = {groups[gi]["id"]: blocks[bi]
                                for gi, bi in zip(selected_groups, selected_blocks)}
        if best:
            return best
    return {}


def propose(ctx: Ctx) -> dict:
    """Measure the line-pair blocks in this scan and build ROIs on what was
    found. The definition supplies nominal positions/frequencies for identifying
    the blocks and as a fallback — it does not fix where the ROIs land."""
    lp = ctx.pdef.linepairs
    roi_mm = lp.get("roi_size_mm", 12.6)
    nominal_angle = lp.get("strip_angle_deg", 45.0)
    try:
        blocks, strip_angle = detect_line_blocks(ctx)
        assigned = _match_blocks_to_groups(ctx, blocks,
                                           strip_angle if strip_angle is not None else nominal_angle)
    except Exception:
        blocks, strip_angle, assigned = [], None, {}
    if strip_angle is None:
        strip_angle = nominal_angle

    # The guide places the ROI with its CORNERS on the nub markers of the two
    # frame lines and on the strip's centre line, i.e. rotated 45 deg from the
    # strip axis rather than square to it.
    roi_offset = lp.get("roi_angle_offset_deg", 45.0)

    groups = []
    for g in lp["groups"]:
        b = assigned.get(g["id"])
        detected = b is not None
        center = b["center_mm"] if detected else list(g["center_mm"])
        # The profile must cross the printed lines, so it follows the measured
        # modulation direction. The ROI is a separate thing: it is placed the
        # way the guide specifies, offset from the strip axis.
        mod_dir = b["mod_dir_deg"] if detected else (strip_angle + 90.0)
        roi_angle = (strip_angle + roi_offset) % 180.0
        u = np.array([np.cos(np.deg2rad(mod_dir)), np.sin(np.deg2rad(mod_dir))])
        half_len = roi_mm / 2 + 2.0
        p0 = np.asarray(center, float) - half_len * u
        p1 = np.asarray(center, float) + half_len * u
        angle = roi_angle
        entry = {
            "id": g["id"], "freq_lp_mm": g["freq_lp_mm"],
            "nominal_center_mm": list(g["center_mm"]), "detected": detected,
            "positioning": b.get("positioning") if detected else None,
            "roi": rect_roi(ctx, center, (roi_mm, roi_mm), angle,
                            roi_id=f"linepairs/{g['id']}"),
            "profile_seg": segment(ctx, p0, p1, f"linepairs/{g['id']}/profile"),
        }
        if detected:
            entry["measured"] = {
                "freq_lp_mm": b["freq_lp_mm"], "mod_dir_deg": b["mod_dir_deg"],
                "offset_from_nominal_mm": float(np.hypot(
                    center[0] - g["center_mm"][0], center[1] - g["center_mm"][1])),
            }
        groups.append(entry)
    return {"groups": groups, "strip_angle_deg": float(strip_angle),
            "roi_size_mm": roi_mm, "roi_angle_offset_deg": float(roi_offset),
            "n_blocks_detected": len(blocks)}


def profile_for_center(ctx: Ctx, center_mm, roi_size_mm: float, seg_id: str,
                       fallback_dir_deg: float | None = None) -> dict:
    """Profile segment for a group ROI centred at ``center_mm``.

    Used when the user drags a line-pair ROI: the profile follows the square and
    its direction is re-measured at the new position, so it still crosses the
    printed lines there."""
    measured = _measure_pattern_2dfft(ctx, center_mm)
    if measured is not None:
        _, dir_px = measured
        # the FFT works in image space; express the direction in phantom mm
        d = np.linalg.solve(ctx.T.A, np.asarray(dir_px, float))
        n = float(np.linalg.norm(d))
        direction = d / n if n > 1e-9 else np.array([1.0, 0.0])
    else:
        a = np.deg2rad(fallback_dir_deg if fallback_dir_deg is not None else 0.0)
        direction = np.array([np.cos(a), np.sin(a)])
    half_len = roi_size_mm / 2 + 2.0
    c = np.asarray(center_mm, float)
    return segment(ctx, c - half_len * direction, c + half_len * direction,
                   seg_id)


def _measure_pattern_2dfft(ctx: Ctx, center_mm, half_mm: float = 5.0):
    """Dominant spatial frequency and orientation of the line pattern via 2-D FFT
    of an image-space patch. Returns (freq_lp_mm, direction_unit_px) or None.

    The direction is perpendicular to the printed lines (the axis along which the
    pattern oscillates), expressed in image-pixel space."""
    T = ctx.T
    c_px = np.asarray(T.mm_to_px(center_mm), float)
    h = int(half_mm * T.px_per_mm)
    cx, cy = int(round(c_px[0])), int(round(c_px[1]))
    if (cy - h < 0 or cx - h < 0 or cy + h > ctx.pixels.shape[0]
            or cx + h > ctx.pixels.shape[1]):
        return None
    patch = ctx.pixels[cy - h:cy + h, cx - h:cx + h].astype(float)
    patch = patch - patch.mean()
    win = np.outer(np.hanning(patch.shape[0]), np.hanning(patch.shape[1]))
    F = np.abs(np.fft.fftshift(np.fft.fft2(patch * win)))
    ny, nx = F.shape
    fy = np.fft.fftshift(np.fft.fftfreq(ny))          # cycles / px
    fx = np.fft.fftshift(np.fft.fftfreq(nx))
    FX, FY = np.meshgrid(fx, fy)
    fmag_mm = np.hypot(FX, FY) / T.mm_per_px          # cycles / mm
    band = (fmag_mm > 0.85) & (fmag_mm < 2.6)
    if not band.any():
        return None
    Fb = np.where(band, F, 0)
    iy, ix = np.unravel_index(int(np.argmax(Fb)), F.shape)
    # significance: peak must clearly exceed the band median
    band_vals = F[band]
    if F[iy, ix] < np.median(band_vals) * 8:
        return None
    fvec = np.array([FX[iy, ix], FY[iy, ix]])
    freq = float(np.hypot(*fvec) / T.mm_per_px)
    direction = fvec / np.linalg.norm(fvec)
    return freq, direction


def _analyze_profile(ctx: Ctx, seg: dict, freq_lp_mm: float,
                     center_mm=None):
    """Extract the line-pattern profile and fit a uniform grid to line peaks.

    The profile direction is taken from the 2-D FFT measurement when available
    (robust to strip-angle errors); the nominal segment is the fallback."""
    T = ctx.T
    p0 = np.asarray(seg["p0_px"], float)
    p1 = np.asarray(seg["p1_px"], float)
    half_len = float(np.linalg.norm(p1 - p0)) / 2.0

    # The ROI is the handle the user drags, so it decides WHERE the profile is
    # sampled. Sampling at the segment's own centre would leave the numbers
    # behind at the old position whenever an ROI is moved.
    c_px = np.asarray(T.mm_to_px(center_mm), float) if center_mm is not None \
        else (p0 + p1) / 2.0

    measured = None
    if center_mm is not None and not seg.get("manually_adjusted"):
        measured = _measure_pattern_2dfft(ctx, center_mm)
    if measured is not None:
        freq_meas, direction = measured
        # keep the profile pointing the same general way as the nominal segment
        if np.dot(direction, p1 - p0) < 0:
            direction = -direction
    else:
        # no usable spectral peak, or the user set the direction by hand
        d = p1 - p0
        direction = d / max(float(np.linalg.norm(d)), 1e-9)
    p0 = c_px - direction * half_len
    p1 = c_px + direction * half_len
    length_px = float(np.linalg.norm(p1 - p0))
    n = max(int(length_px * 2), 64)          # ~0.5 px sampling
    ts, vals = sample_profile(ctx.pixels, p0, p1, n,
                              avg_half_width=4.0 * T.px_per_mm, n_avg=15)
    pos_mm = ts * T.mm_per_px
    nominal_pitch = 1.0 / freq_lp_mm

    # detrend (background slope) then find bright line peaks
    detr = vals - ndimage.gaussian_filter1d(vals, 3.0 * nominal_pitch /
                                            (pos_mm[1] - pos_mm[0]))
    min_dist = max(int(0.6 * nominal_pitch / (pos_mm[1] - pos_mm[0])), 2)
    pk = find_peaks_1d(detr, min_distance=min_dist, threshold_rel=0.45)
    peaks_mm = np.array([np.interp(centroid_peak(detr, i, max(min_dist // 2, 1)),
                                   np.arange(len(pos_mm)), pos_mm) for i in pk])

    result = {
        "profile": {"pos_mm": pos_mm.tolist(), "value": vals.tolist(),
                    "detrended": detr.tolist()},
        "n_lines": int(len(peaks_mm)),
        "peaks_mm": peaks_mm.tolist(),
        "nominal_pitch_mm": nominal_pitch,
        "fft_freq_lp_mm": measured[0] if measured else None,
    }
    if len(peaks_mm) >= 4:
        gaps = np.diff(peaks_mm)
        # Coarse plausibility against the nominal, then a robust second pass
        # against the MEASURED median gap. The block edges throw off short
        # spurious gaps that sit inside a nominal-only window and would drag the
        # fitted pitch down by several percent.
        coarse = gaps[(gaps > 0.55 * nominal_pitch) & (gaps < 1.45 * nominal_pitch)]
        med_gap = float(np.median(coarse)) if coarse.size else nominal_pitch
        ok = np.abs(gaps - med_gap) <= 0.18 * med_gap

        best_s, best_e = 0, 0
        s = 0
        for i in range(len(ok)):
            if not ok[i]:
                s = i + 1
            elif i + 1 - s > best_e - best_s:
                best_s, best_e = s, i + 1
        run = peaks_mm[best_s:best_e + 1]

        if len(run) >= 4:
            def fit(vals):
                idx = np.arange(len(vals))
                A = np.vstack([idx, np.ones_like(idx)]).T
                coef, *_ = np.linalg.lstsq(A, vals, rcond=None)
                return coef, vals - (A @ coef)

            coef, resid = fit(run)
            # one robust pass: drop any line more than 4 MAD off the grid
            mad = float(np.median(np.abs(resid - np.median(resid)))) + 1e-9
            keep = np.abs(resid - np.median(resid)) <= max(4.0 * mad,
                                                           0.08 * med_gap)
            if keep.sum() >= 4 and keep.sum() < len(run):
                # renumber the kept lines by their grid index so gaps stay right
                idx_all = np.round((run - run[0]) / coef[0]).astype(int)
                kept_idx = idx_all[keep]
                A = np.vstack([kept_idx, np.ones_like(kept_idx)]).T
                coef, *_ = np.linalg.lstsq(A, run[keep], rcond=None)
                run = run[keep]
                resid = run - (A @ coef)

            pitch = float(coef[0])
            result.update({
                "used_lines": int(len(run)),
                "grid_peaks_mm": run.tolist(),
                "measured_pitch_mm": pitch,
                "measured_freq_lp_mm": 1.0 / pitch if pitch > 0 else None,
                "pitch_dev_pct": 100.0 * (pitch - nominal_pitch) / nominal_pitch,
                "grid_residuals_mm": resid.tolist(),
                "residual_rms_mm": float(np.sqrt(np.mean(resid ** 2))),
                "median_gap_mm": med_gap,
            })
    return result


def compute(ctx: Ctx, geometry: dict) -> dict:
    tol_pitch = ctx.pdef.tolerances.get("linepair_pitch_dev_pct", 10.0)
    rows = []
    for g in geometry["groups"]:
        s = stats_for_roi(ctx, g["roi"])
        prof = _analyze_profile(ctx, g["profile_seg"], g["freq_lp_mm"],
                                center_mm=g["roi"]["center_mm"])
        pitch_dev = prof.get("pitch_dev_pct")
        status, reason = "pass", ""
        if pitch_dev is None:
            status = "warn"
            n_lines = prof.get("n_lines", 0)
            used = prof.get("used_lines")
            if n_lines < 4:
                reason = (f"no line pattern found on the profile — only "
                          f"{n_lines} peak(s) detected. The ROI is probably not "
                          f"on the group, or the pattern is not resolved at "
                          f"this dose/geometry.")
            elif not used:
                reason = (f"{n_lines} peaks found but their spacing is too "
                          f"irregular to fit a line grid, so pitch could not be "
                          f"measured.")
            else:
                reason = "pitch could not be fitted from the profile."
        elif abs(pitch_dev) > tol_pitch:
            status = "fail"
            reason = (f"measured pitch {prof['measured_pitch_mm']:.4f} mm is "
                      f"{pitch_dev:+.1f}% from the nominal "
                      f"{prof['nominal_pitch_mm']:.4f} mm "
                      f"(tolerance +/-{tol_pitch:g}%). Either this ROI is on a "
                      f"different group than its label, or the phantom differs "
                      f"from the definition.")
        else:
            reason = (f"pitch {prof['measured_pitch_mm']:.4f} mm, "
                      f"{pitch_dev:+.1f}% from nominal (within "
                      f"+/-{tol_pitch:g}%).")
        rows.append({
            "id": g["id"], "freq_lp_mm": g["freq_lp_mm"],
            "mean": s["mean"], "std": s["std"], "n": s["n"],
            "linearity": prof, "status": status, "reason": reason,
        })
    overall = "pass"
    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "warn" for r in rows):
        overall = "warn"
    bad = [r["id"] for r in rows if r["status"] != "pass"]
    reasons = [f"{r['id']}: {r['reason']}" for r in rows
               if r["status"] != "pass" and r["reason"]]
    if overall == "pass":
        reasons = ["every group's measured pitch is within "
                   f"+/-{tol_pitch:g}% of nominal."]
    return {"rows": rows, "status": overall, "reasons": reasons,
            "affected": bad, "pitch_tolerance_pct": tol_pitch}
