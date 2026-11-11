"""Centre-seeded square search; structural evidence, never QA acceptance metrics."""
import numpy as np
from scipy import ndimage, optimize
from skimage.filters import threshold_otsu


def square(center, side, angle):
    a = np.deg2rad(angle)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    return np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) @ R.T * side / 2 + center


def detect(pixels, side_px_prior=None):
    """Return ranked square hypotheses, including dark interiors in bright fields."""
    h, w = pixels.shape
    ds = max(1, int(np.ceil(max(h, w) / 384)))
    small = ndimage.gaussian_filter(pixels[::ds, ::ds].astype(float), 1)
    span = np.percentile(small, 99) - np.percentile(small, 1)
    if not np.isfinite(span) or span <= 1e-9:
        raise ValueError('No phantom boundary evidence; place the corners manually.')
    image = small / span
    gradient = np.hypot(*np.gradient(ndimage.gaussian_filter(image, .7)))
    limit = np.array([w / ds, h / ds])
    seeds = []
    if side_px_prior is not None and np.isfinite(side_px_prior) and side_px_prior > 0:
        for factor in (1., 1.06, 1.12):
            for angle in range(0, 90, 15):
                seeds.append([*(limit/2), side_px_prior/ds*factor, angle])
    for fraction in (.55, .75, .92):
        for angle in range(0, 90, 15):
            seeds.append([* (limit / 2), min(limit) * fraction, angle])
    # Regions only propose hypotheses. Never fill a border-connected bright
    # region: its hole may be the entire phantom we are trying to locate.
    threshold = threshold_otsu(small)
    for foreground in (small > threshold, small < threshold):
        labels, count = ndimage.label(foreground)
        objects = ndimage.find_objects(labels)
        for i, bounds in enumerate(objects):
            if bounds is None:
                continue
            yy, xx = bounds
            if min(xx.start, yy.start) == 0 or xx.stop == small.shape[1] or yy.stop == small.shape[0]:
                continue
            if (yy.stop-yy.start)*(xx.stop-xx.start) < .12 * small.size:
                continue
            ys, xs = np.where(labels == i+1)
            from .registration import _min_area_angle
            angle = _min_area_angle(np.c_[xs, ys])
            # _min_area_angle rotates image points into their rectangle frame.
            a = np.deg2rad(angle)
            R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
            q = np.c_[xs, ys] @ R.T
            side = float(np.mean(np.ptp(q, axis=0)))
            for expansion in (1., 1.04, 1.08):
                seeds.append([*np.mean([q.min(0),q.max(0)],axis=0) @ R,
                              side*expansion, -angle])
    f = np.linspace(-.42,.42,35)
    def objective(p):
        x,y,side,angle = p
        corners = square([x,y], side, angle)
        if side < min(limit)*.3 or side > max(limit)*1.1:
            return 1e3
        if np.any(corners < 2) or np.any(corners > limit-3):
            return 1e3
        a=np.deg2rad(angle); u=np.array([np.cos(a),np.sin(a)]); v=np.array([-u[1],u[0]])
        scores=[]
        for normal,tangent in ((u,v),(-u,v),(v,u),(-v,u)):
            points=np.array([x,y])+normal*side/2 + f[:,None]*tangent*side
            values=ndimage.map_coordinates(gradient,[points[:,1],points[:,0]],order=1,mode='constant')
            scores.append(np.median(values))
        return -float(np.mean(scores)+np.min(scores))
    # A centre prior supplies the first guess; spatial evidence may move it.
    seeds.sort(key=objective)
    candidates=[]
    for seed in seeds[:12]:
        fit=optimize.minimize(objective,seed,method='Powell', options={'maxiter':55,'xtol':.02,'ftol':1e-5})
        if fit.fun >= -0.008:
            continue
        p=fit.x
        if any(np.linalg.norm(p[:2]-q['pose'][:2])<2 and abs(p[2]-q['pose'][2])<3 for q in candidates):
            continue
        candidates.append({'pose':p,'corners':square(p[:2],p[2],p[3])*ds,
                           'coarse_angle_deg':float(p[3]),'boundary_score':float(-fit.fun)})
    if not candidates:
        raise ValueError('No supported square phantom boundary; place the corners manually.')
    return sorted(candidates,key=lambda c:-c['boundary_score'])


def structural_score(pixels, T, pdef):
    """Outline agreement independent of insert direction and measured QA values."""
    from .analysis.positioning import rectangle_outline, rotation
    stride=max(1,int(np.ceil(max(pixels.shape)/700)))
    small=ndimage.gaussian_filter(pixels[::stride,::stride].astype(float),.8)
    scale=max(float(np.percentile(small,95)-np.percentile(small,5)),1e-9)
    def sample(points):
        px=T.mm_to_px(points)/stride
        return ndimage.map_coordinates(small,[px[:,1],px[:,0]],order=1,mode='constant',cval=np.nan)
    def outline(center,size,angle=0, ridge=True):
        pts,normals,sections=rectangle_outline(size,16)
        pts=pts @ rotation(angle).T + center; normals=normals @ rotation(angle).T
        best=0.
        for dx,dy in ((0,0),(-4,0),(4,0),(0,-4),(0,4)):
            q=pts+[dx,dy]
            a,b,c=sample(q),sample(q-normals*2),sample(q+normals*2)
            response=np.maximum(a-np.maximum(b,c),0) if ridge else np.abs(b-c)
            sides=[np.nanmedian(response[sections==i])/scale for i in np.unique(sections)]
            if np.isfinite(sides).all():
                best=max(best,float(np.mean(np.clip(sides,0,.3))))
        return best
    uniformity=np.mean([outline(s['center_mm'],[pdef.uniformity['size_mm']]*2) for s in pdef.uniformity['squares']])
    lc=pdef.lowcontrast; w=pdef.wedge
    lowcontrast=outline(lc['center_mm'],lc['size_mm'],lc['angle_deg'],True)
    lowcontrast=max(lowcontrast, .5*outline(lc['center_mm'],lc['size_mm'],lc['angle_deg'],False))
    wedge=outline([w['center_x_mm'],(w['y_top_mm']+w['y_bottom_mm'])/2],
                  [w.get('frame_width_mm',20),w['y_top_mm']-w['y_bottom_mm']],ridge=False)
    lp=pdef.linepairs; centers=np.array([g['center_mm'] for g in lp['groups']]); angle=lp.get('strip_angle_deg',45)
    local=(centers-centers.mean(0)) @ rotation(angle)
    strip=outline(centers.mean(0),np.ptp(local,axis=0)+lp.get('roi_size_mm',12.6),angle,False)
    parts=dict(uniformity=float(uniformity),lowcontrast=lowcontrast,wedge=wedge,linepairs=strip)
    parts['total']=float(uniformity*4 + lowcontrast*2 + wedge + strip)
    return parts
