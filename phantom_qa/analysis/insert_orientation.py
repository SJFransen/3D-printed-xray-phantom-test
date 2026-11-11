"""Local insert reflection, separate from whole-phantom registration.

Only confident design-order evidence identifies a low-contrast insert. A
symmetrical outline alone cannot reveal whether an insert has been turned over.
The evidence is diagnostic, never a change to CNR or its acceptance thresholds.
"""
import numpy as np
from scipy import ndimage, stats


def read(ctx, center, angle, shift=(0.,0.), grid_angle=0.):
    from .positioning import rotation
    lc=ctx.pdef.lowcontrast
    grid_center=np.asarray(center)+rotation(angle) @ shift
    R=rotation(angle+grid_angle)
    # Sample identical object/background areas for each physical hypothesis.
    axis=np.linspace(-8.,8.,25)
    xx,yy=np.meshgrid(axis,axis)
    local=np.c_[xx.ravel(),yy.ravel()]
    radial=np.linalg.norm(local,axis=1)
    obj=radial<lc.get('roi_dia_mm',7.)/2
    bg=(radial>lc.get('bg_inner_dia_mm',12.)/2)&(radial<lc.get('bg_outer_dia_mm',16.)/2)
    candidates=[]
    for fx,fy in ((False,False),(True,False),(False,True),(True,True)):
        strengths=[]
        for c in lc['circles']:
            uv=np.array([c['u_mm']*(-1 if fx else 1),c['v_mm']*(-1 if fy else 1)])
            px=ctx.T.mm_to_px((local+uv) @ R.T + grid_center)
            values=ndimage.map_coordinates(ctx.pixels,[px[:,1],px[:,0]],order=1,mode='constant',cval=np.nan)
            a,b=values[obj],values[bg]
            noise=np.sqrt(np.var(a)+np.var(b))
            strengths.append(abs(np.mean(a)-np.mean(b))/max(noise,1e-9))
        rho=float(stats.spearmanr([c['level'] for c in lc['circles']],strengths).statistic) if np.isfinite(strengths).all() and np.ptp(strengths)>1e-9 else 0.
        candidates.append(dict(flip_x=fx,flip_y=fy,correlation=rho,
                               visible=sum(s>.6 for s in strengths)))
    candidates.sort(key=lambda c:-c['correlation'])
    best=candidates[0]; margin=best['correlation']-candidates[1]['correlation']
    confident=best['visible']>=4 and best['correlation']>=.8 and margin>=.25
    return {"flip_x":best['flip_x'] if confident else False,
            "flip_y":best['flip_y'] if confident else False,
            "source":"measured" if confident else "undetermined",
            "correlation":best['correlation'],"margin":margin,
            "candidates":candidates}
