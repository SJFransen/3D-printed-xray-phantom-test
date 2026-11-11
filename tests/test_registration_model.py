"""Regression for bright detector surrounds, rotation and independent inserts."""
from pathlib import Path
import numpy as np
import pytest
from phantom_qa import ingest, registration
from phantom_qa.analysis import lowcontrast
from phantom_qa.analysis.common import Ctx
from phantom_qa.analysis.insert_orientation import read
from phantom_qa.phantom_def import load_default
from test_registration import synth_square
from test_roi_orientation import client, mod, aid


def test_dark_face_inside_bright_surround_is_not_the_detector():
    image, truth = synth_square(angle_deg=12, side_px=700, img=1100, bright=1000, bg=3000)
    got = registration.detect_phantom_rect(image)['corners']
    for corner in truth:
        assert np.min(np.linalg.norm(got-corner,axis=1)) < 2


def test_blank_image_rejects_registration():
    with pytest.raises(ValueError,match='boundary'):
        registration.register(np.full((500,600),1000.), load_default())


REAL = Path(__file__).resolve().parents[2] / '..' / 'images' / 'Test Bucky 1 LZR QC Leeds' / 'DICOM' / '000002'


@pytest.mark.skipif(not REAL.exists(),reason='local DICOM regression fixture is not installed')
@pytest.mark.parametrize('turn,mirror',[(0,False),(1,False),(2,True),(3,True)])
def test_real_scan_registers_phantom_not_detector(turn,mirror):
    scan=ingest.load_path(str(REAL))[0]
    image=np.rot90(scan.pixels,turn)
    if mirror:
        image=image[:,::-1]
    reg=registration.register(image,load_default())
    # Independently reviewed outer-frame annotations on the original scan.
    truth=np.array([[90,310],[2420,281],[2454,2614],[120,2648]],float)
    h,w=scan.shape
    for _ in range(turn):
        truth=np.c_[truth[:,1],w-1-truth[:,0]]
        h,w=w,h
    if mirror:
        truth[:,0]=w-1-truth[:,0]
    for corner in truth:
        assert np.min(np.linalg.norm(reg.corners_px-corner,axis=1)) < 12
    assert reg.transform.mirrored == mirror
    singular=np.linalg.svd(reg.transform.A,compute_uv=False)
    assert singular[0] == pytest.approx(singular[1],rel=1e-10)
    assert reg.residual_rms_mm < 1.
    # Physical feature identities, not only an unlabelled square, must rotate.
    centre=np.asarray(reg.transform.mm_to_px([0,0]))
    assert np.linalg.norm(centre-[w/2,h/2]) < min(h,w)*.15


@pytest.mark.parametrize('fx,fy',[(False,False),(True,False),(False,True),(True,True)])
def test_lowcontrast_local_reflection_preserves_group_geometry(fx,fy,pdef):
    lc=pdef.lowcontrast
    T=registration.Transform(np.array([[4.,0],[0,-4.]]),np.array([230.,200.]))
    image=np.full((420,460),2000.)
    yy,xx=np.indices(image.shape)
    for c in lc['circles']:
        uv=[c['u_mm']*(-1 if fx else 1),c['v_mm']*(-1 if fy else 1)]
        px=T.mm_to_px(uv)
        image[(xx-px[0])**2+(yy-px[1])**2 < (lc['circle_dia_mm']*2)**2] -= 15*c['level']
    image += np.random.default_rng(4).normal(0,2,image.shape)
    ctx=Ctx(image,T,pdef)
    orientation=read(ctx,[0,0],0)
    assert orientation['source']=='measured'
    assert (orientation['flip_x'],orientation['flip_y'])==(fx,fy)
    circles=lowcontrast.circles_for_block(ctx,[0,0],0,insert_orientation=orientation)
    for c,d in zip(lc['circles'],circles):
        assert d['roi']['center_mm']==pytest.approx([c['u_mm']*(-1 if fx else 1),c['v_mm']*(-1 if fy else 1)])
    assert [c['id'] for c in circles]==[c['id'] for c in lc['circles']]


def test_unobservable_insert_is_not_silently_declared_flipped(pdef):
    ctx=Ctx(np.ones((500,500)),registration.Transform(np.diag([4.,-4.]),np.array([250.,250.])),pdef)
    assert read(ctx,[0,0],0)['source']=='undetermined'


def test_reversed_wedge_uses_boundary_structure_not_response_order():
    from test_positioning import synthetic_ctx
    from phantom_qa.analysis import wedge
    from scipy import ndimage
    ctx,truth=synthetic_ctx()
    forward=wedge.propose(ctx)
    assert forward['detected']
    center,angle,_=truth['wedge']
    yy,xx=np.indices(ctx.pixels.shape)
    mm=ctx.T.px_to_mm(np.c_[xx.ravel(),yy.ravel()])
    local=mm-np.asarray(center)
    mask=(abs(local[:,0])<25)&(abs(local[:,1])<90)
    source=ctx.T.mm_to_px(2*np.asarray(center)-mm[mask])
    reflected=ctx.pixels.copy().ravel()
    reflected[mask]=ndimage.map_coordinates(ctx.pixels,[source[:,1],source[:,0]],order=1)
    flipped=Ctx(reflected.reshape(ctx.pixels.shape),ctx.T,ctx.pdef)
    reverse=wedge.propose(flipped)
    assert reverse['detected']
    assert reverse['positioning']['insert_reversed']
    for before,after in zip(forward['steps'],reverse['steps']):
        np.testing.assert_allclose(after['roi']['center_mm'],
            2*np.asarray(center)-np.asarray(before['roi']['center_mm']),atol=1.)


def test_manual_insert_reflection_is_undoable_and_kept_on_reposition(client,mod,aid):
    # Existing authenticated correction fixtures protect real route semantics.
    ctx=Ctx(np.ones((64,64)), registration.Transform(np.diag([1.,-1.]),np.array([32.,32.])),load_default())
    from phantom_qa.analysis.common import rect_roi
    from phantom_qa import pipeline
    geom={'lowcontrast': {'block':rect_roi(ctx,[0,0],[80,40],0,'lowcontrast/block'),
                         'circles':lowcontrast.circles_for_block(ctx,[0,0],0)}}
    mod.store.set_geometry_baseline(aid,pipeline.to_jsonable(geom))
    response=client.post(f'/api/analyses/{aid}/lowcontrast_block',json={'flip_x':True})
    assert response.status_code==200
    reflected=response.json()['lowcontrast']
    assert reflected['insert_orientation']=={'flip_x':True,'flip_y':False,'source':'user'}
    expected=-ctx.pdef.lowcontrast['circles'][0]['u_mm']
    assert reflected['circles'][0]['roi']['center_mm'][0]==pytest.approx(expected)
    moved=client.post(f'/api/analyses/{aid}/lowcontrast_block',json={'center_px':[33,32]})
    assert moved.status_code==200
    assert moved.json()['lowcontrast']['insert_orientation']['flip_x']
    client.post(f'/api/analyses/{aid}/geometry/undo')
    client.post(f'/api/analyses/{aid}/geometry/undo')
    assert not (mod.store.get(aid)['geometry']['lowcontrast'].get('insert_orientation') or {}).get('flip_x')
