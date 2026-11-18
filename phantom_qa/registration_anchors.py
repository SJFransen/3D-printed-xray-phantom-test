"""Image-space registration anchors independent of an outer-box hypothesis.

Printed square dimensions establish scale; the wedge and remaining layout
establish identity/orientation. No QA measurement or pass threshold drives fit.
"""
import numpy as np
from scipy import ndimage, optimize

from .registration_model import square, structural_score


def refine_square(pixels, corners):
    """Fit printed line centres at full resolution, rather than their edges."""
    from .features import locate_line_center, fit_line_tls
    lines = []
    for i in range(4):
        start, end = corners[i], corners[(i+1) % 4]
        direction = (end-start) / np.linalg.norm(end-start)
        normal = np.array([-direction[1], direction[0]])
        points = []
        for f in np.linspace(.15, .85, 17):
            point, strength = locate_line_center(
                pixels, start + f*(end-start), normal,
                search_half=max(8., np.linalg.norm(end-start)*.025),
                avg_half_width=3., n_avg=3)
            if point is not None and strength > 0:
                points.append(point)
        if len(points) < 12:
            return None
        points = np.asarray(points)
        c, u = fit_line_tls(points)
        residual = np.abs((points-c) @ np.array([-u[1], u[0]]))
        keep = residual < max(2., 4*np.median(residual))
        if keep.sum() < 12:
            return None
        lines.append(fit_line_tls(points[keep]))
    refined = []
    for i in range(4):
        c, u = lines[i-1]
        d, v = lines[i]
        try:
            t = np.linalg.solve(np.array([u, -v]).T, d-c)[0]
        except np.linalg.LinAlgError:
            return None
        refined.append(c+t*u)
    return np.asarray(refined)


def square_anchors(pixels):
    """Find enclosed, four-sided bright outlines at arbitrary image positions."""
    from .registration import _min_area_angle
    ds = max(1, int(np.ceil(max(pixels.shape) / 900)))
    image = pixels[::ds, ::ds].astype(float)
    span = np.percentile(image, 99) - np.percentile(image, 1)
    if not np.isfinite(span) or span <= 0:
        return []
    ridge = (ndimage.gaussian_filter(image, .6) -
             ndimage.gaussian_filter(image, 3)) / span
    barrier = ndimage.binary_dilation(ridge > .025, iterations=1)
    labels, _ = ndimage.label(~barrier)
    candidates = []
    for index, bounds in enumerate(ndimage.find_objects(labels), 1):
        if bounds is None:
            continue
        yy, xx = bounds
        if (xx.start == 0 or yy.start == 0 or xx.stop == image.shape[1]
                or yy.stop == image.shape[0]):
            continue
        region = labels[yy, xx] == index
        if not .002 * image.size < region.sum() < .09 * image.size:
            continue
        edge = region & ~ndimage.binary_erosion(region)
        ys, xs = np.where(edge)
        points = np.c_[xs + xx.start, ys + yy.start]
        angle = _min_area_angle(points)
        a = np.deg2rad(angle)
        R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
        q = points @ R.T
        sizes = np.ptp(q, axis=0)
        if min(sizes) < 12 or max(sizes) / min(sizes) > 1.12:
            continue
        if region.sum() / np.prod(sizes) < .85:
            continue
        center = (q.min(0) + q.max(0)) / 2 @ R
        side = float(np.mean(sizes))
        f = np.linspace(.15, .85, 25)

        def responses(p):
            corners = square(p[:2], p[2], p[3])
            values = []
            for i in range(4):
                pts = corners[i] + f[:, None] * (corners[(i+1) % 4] - corners[i])
                v = ndimage.map_coordinates(ridge, [pts[:, 1], pts[:, 0]],
                                             order=1, mode='constant')
                values.append(np.median(v))
            return np.asarray(values)

        seed = [*center, side + 4, -angle]
        bounds_fit = [(center[0]-4, center[0]+4), (center[1]-4, center[1]+4),
                      (side, side+10), (-angle-3, -angle+3)]
        fit = optimize.minimize(lambda p: -float(np.mean(responses(p))), seed,
                                method='Powell', bounds=bounds_fit,
                                options={'maxiter': 35, 'xtol': .01})
        strengths = responses(fit.x)
        if np.min(strengths) > .02:
            corners = refine_square(pixels, square(fit.x[:2], fit.x[2], fit.x[3])*ds)
            if corners is None:
                continue
            candidates.append({'corners': corners,
                               'side_px': float(np.mean(np.linalg.norm(np.roll(corners,-1,axis=0)-corners,axis=1))),
                               'strength': float(np.min(strengths))})
    return candidates


def anchor_transforms(pixels, pdef):
    """Propose transforms from every square; verify central identity via layout."""
    from .registration import candidate_transforms, Transform
    central = next((s for s in pdef.uniformity['squares'] if s['id'] == 'C'), None)
    if central is None:
        return []
    proposals = []
    from .analysis.positioning import rectangle_outline
    ds = max(1, int(np.ceil(max(pixels.shape)/900)))
    image = ndimage.gaussian_filter(pixels[::ds,::ds].astype(float), .5)
    span = max(np.percentile(image,99)-np.percentile(image,1), 1e-9)
    w = pdef.wedge
    wedge_points, normals, sections = rectangle_outline(
        [w.get('frame_width_mm',20), w['y_top_mm']-w['y_bottom_mm']], 25)
    wedge_points += [w['center_x_mm'], (w['y_top_mm']+w['y_bottom_mm'])/2]

    def wedge_support(T):
        def sample(points):
            px = T.mm_to_px(points)/ds
            return ndimage.map_coordinates(image,[px[:,1],px[:,0]],order=1,
                                            mode='constant',cval=np.nan)
        best = 0.
        for dx in (-2.,0.,2.):
            for dy in (-2.,0.,2.):
                q = wedge_points+[dx,dy]
                a,b,c = sample(q),sample(q-normals*2),sample(q+normals*2)
                response = np.maximum(np.abs(b-c),np.abs(a-(b+c)/2))/span
                sides = []
                for i in range(4):
                    v = response[sections==i]
                    v = v[np.isfinite(v)]
                    sides.append(float(np.median(v)) if len(v) >= 20 else 0.)
                best = max(best, min(sides))
        return best

    for anchor in square_anchors(pixels):
        for T, meta in candidate_transforms(anchor['corners'], pdef.uniformity['size_mm']):
            u, s, vt = np.linalg.svd(T.A)
            if max(s)/min(s) > 1.04:
                continue
            T = Transform((u @ vt)*np.mean(s), T.t)
            half = pdef.uniformity['size_mm']/2
            nominal = np.array([[-half,half],[half,half],[half,-half],[-half,-half]])
            residual = float(np.sqrt(np.mean(np.sum((T.mm_to_px(nominal)-
                anchor['corners'][meta['assignment']])**2,axis=1)))*T.mm_per_px)
            if residual > .6:
                continue
            T = Transform(T.A, T.t - T.A @ np.asarray(central['center_mm']))
            parts = structural_score(pixels, T, pdef)
            # The wedge is mandatory; a third structure resolves the square's
            # symmetry and avoids treating a peripheral square as the centre.
            if (parts['wedge'] < .035 or parts['uniformity'] < .025
                    or max(parts['lowcontrast'], parts['linepairs']) < .012):
                continue
            support = wedge_support(T)
            if support < .02:
                continue
            anchor = {**anchor, 'wedge_support': support, 'square_residual_mm': residual}
            proposals.append((T, meta, parts, anchor))
    return proposals
