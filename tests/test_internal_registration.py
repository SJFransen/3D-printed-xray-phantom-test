"""Independent anchor and ROI regressions; also runnable with unittest."""
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from phantom_qa import registration, pipeline, ingest
from phantom_qa.analysis.common import Ctx, rect_roi, stats_for_roi
from phantom_qa.analysis.positioning import rotation, build_templates
from phantom_qa.phantom_def import load_default
from phantom_qa.registration_anchors import anchor_transforms, square_anchors


def image_fixture(angle=0, mirror=False, wedge=True):
    """Fixed physical features, independent of the detector's inferred box."""
    p = load_default()
    A = rotation(angle) @ np.diag([2., 2. if mirror else -2.])
    T = registration.Transform(A, np.array([430., 360.]))
    yy, xx = np.indices((720, 680))
    mm = T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(720,680,2)
    image = np.full((720,680), 1000.)

    def frame(center, size, angle=0, value=3000):
        q = (mm-center) @ rotation(angle)
        half = np.asarray(size)/2
        distance = np.max(np.abs(q)-half, axis=2)
        image[np.abs(distance) < .65] = value

    for s in p.uniformity['squares']:
        frame(s['center_mm'], [p.uniformity['size_mm']]*2)
    if wedge:
        w = p.wedge
        frame([w['center_x_mm'], (w['y_top_mm']+w['y_bottom_mm'])/2],
              [w['frame_width_mm'], w['y_top_mm']-w['y_bottom_mm']], value=100)
    lc = p.lowcontrast
    frame(lc['center_mm'], lc['size_mm'], lc['angle_deg'])
    image += np.random.default_rng(12).normal(0, 2, image.shape)
    return image, T, p


class InternalRegistrationTests(unittest.TestCase):
    def test_existing_outer_edge_accuracy(self):
        for angle, bright, background in ((0,3000,250),(1.7,3000,250),
                                          (-3.2,3000,250),(12,1000,3000)):
            with self.subTest(angle=angle):
                n, side = 1100, 700
                yy,xx = np.indices((n,n),dtype=float)
                a = np.deg2rad(angle)
                x = (xx-n/2)*np.cos(a)+(yy-n/2)*np.sin(a)
                y = -(xx-n/2)*np.sin(a)+(yy-n/2)*np.cos(a)
                image = np.where((abs(x)<=side/2)&(abs(y)<=side/2),bright,background).astype(float)
                image += np.random.default_rng(1).normal(0,15,image.shape)
                truth = np.array([[-1,-1],[1,-1],[1,1],[-1,1]]) @ rotation(angle).T*side/2+n/2
                got = registration.detect_phantom_rect(image)['corners']
                for point in truth:
                    self.assertLess(np.min(np.linalg.norm(got-point,axis=1)),1.)

    def test_manual_corners_skip_anchor_detection(self):
        image, truth, p = image_fixture()
        L = p.side_mm/2
        corners = truth.mm_to_px([[-L,L],[L,L],[L,-L],[-L,-L]])
        with patch('phantom_qa.registration_anchors.anchor_transforms',side_effect=AssertionError('manual override')):
            reg = registration.register(image,p,corners_hint=corners)
        self.assertEqual(reg.score['method'],'manual_corners')
        np.testing.assert_allclose(reg.corners_px,corners)

    def test_blank_has_no_anchors(self):
        self.assertEqual(square_anchors(np.ones((300,300))), [])

    def test_missing_outer_side_is_inferred_without_sampling_padding(self):
        image = np.full((600,600),250.)
        image[50:551,150:] = 3000.
        corners = np.array([[150,50],[650,50],[650,550],[150,550]],float)
        rect = registration.detect_phantom_rect(image,initial_corners=corners)
        self.assertEqual(rect['observed_edges'],[True,False,True,True])
        self.assertTrue(rect['inferred_outer_box'])
        self.assertGreater(rect['corners'][:,0].max(),600)

    def test_blank_edges_are_not_manufactured(self):
        with self.assertRaisesRegex(ValueError,'support'):
            registration.detect_phantom_rect(np.ones((600,600)),initial_corners=
                np.array([[80,80],[520,80],[520,520],[80,520]],float))

    def test_missing_wedge_cannot_verify_anchors(self):
        image, _, p = image_fixture(wedge=False)
        self.assertEqual(anchor_transforms(image, p), [])

    def test_partial_frame_rotation_and_mirror(self):
        for angle, mirror in ((0,False),(12,False),(90,True),(180,True)):
            with self.subTest(angle=angle, mirror=mirror):
                image, truth, p = image_fixture(angle, mirror)
                # Force outer detection to fail: anchors must work independently.
                with patch('phantom_qa.registration_model.detect', side_effect=ValueError('clipped')):
                    reg = registration.register(image, p)
                self.assertEqual(reg.score['method'], 'internal_square_wedge')
                self.assertEqual(reg.transform.mirrored, mirror)
                self.assertAlmostEqual(reg.transform.px_per_mm, truth.px_per_mm, delta=.02)
                points = np.array([[0,0],[-80,80],[60,-60]])
                error = np.linalg.norm(reg.transform.mm_to_px(points)-truth.mm_to_px(points), axis=1)
                self.assertLess(error.max(), 3.)
                templates = build_templates(Ctx(image, reg.transform, p))
                expected = truth.mm_to_px(p.lowcontrast['center_mm'])
                self.assertLess(np.linalg.norm(np.array(templates['lowcontrast/block']['center_px'])-expected), 3.)

    def test_inferred_box_is_not_a_dimension_measurement(self):
        from phantom_qa.analysis import geometry
        image, T, p = image_fixture()
        reg = registration.Registration(T, np.array([[0,0],[1,0],[1,1],[0,1]]), 0,
                                        {'inferred_outer_box': True})
        ctx = Ctx(image,T,p,reg=reg)
        with patch.object(geometry, '_ruler_probe', return_value={'detected':False}), \
             patch.object(geometry, '_field_edge', return_value={'detected':False}):
            self.assertEqual(geometry.propose(ctx)['corners'], {})

    def test_clipped_measurement_is_rejected(self):
        image,T,p = image_fixture()
        reg = registration.Registration(T, np.zeros((4,2)), 0, {'inferred_outer_box':True})
        ctx = Ctx(image,T,p,reg=reg)
        with self.assertRaisesRegex(ValueError, 'outside'):
            stats_for_roi(ctx, rect_roi(ctx,[140,0],[30,30]))

    def test_zero_collimation_is_unavailable(self):
        image,T,p = image_fixture()
        image[350:370,420:440] = 0
        reg = registration.Registration(T,np.zeros((4,2)),0,{'inferred_outer_box':True})
        ctx = Ctx(image,T,p,reg=reg)
        with self.assertRaisesRegex(ValueError, 'unexposed'):
            stats_for_roi(ctx,rect_roi(ctx,[0,0],[30,30]))

    def test_strong_wrong_outer_box_is_vetoed(self):
        image, truth, p = image_fixture()
        L = p.side_mm/2
        wrong = truth.mm_to_px([[-L,L],[L,L],[L,-L],[-L,-L]])*.8
        from phantom_qa.registration_model import structural_score
        def inflated_score(pixels,T,pdef):
            # Simulate a persuasive field rectangle; anchor detection retains
            # its independent implementation of structural scoring.
            parts = structural_score(pixels,T,pdef)
            return {**parts,'total':10.}
        with patch('phantom_qa.registration_model.detect',return_value=[{'corners':wrong}]), \
             patch.object(registration,'detect_phantom_rect',return_value={'corners':wrong}), \
             patch('phantom_qa.registration_model.structural_score',side_effect=inflated_score):
            reg = registration.register(image,p)
        self.assertEqual(reg.score['method'],'internal_square_wedge')
        self.assertAlmostEqual(reg.transform.px_per_mm,truth.px_per_mm,delta=.02)

    def test_registration_roundtrip_preserves_anchor_metadata(self):
        image,T,p = image_fixture()
        reg = registration.Registration(T,np.zeros((4,2)),0,{'inferred_outer_box':True,
                                                                         'method':'internal_square_wedge'})
        restored = pipeline.registration_from_dict(pipeline.registration_to_dict(reg))
        self.assertEqual(restored.score,reg.score)

    def test_local_dicom_positions(self):
        root = Path(__file__).resolve().parents[2].parent/'images'/'Test Bucky 1 LZR QC Leeds'/'DICOM'
        if not (root/'000039').exists():
            self.skipTest('local DICOM not installed')
        for name in ('000039','000002'):
            scan = ingest.load_path(root/name)[0]
            reg = pipeline.run_stage_a(scan,load_default())
            self.assertFalse(reg.transform.mirrored)
            self.assertLess(reg.residual_rms_mm,1.)
            if name == '000039':
                self.assertEqual(reg.score['method'],'internal_square_wedge')
                self.assertGreater(reg.corners_px[:,0].max(),scan.shape[1])
                self.assertLess(np.linalg.norm(reg.transform.mm_to_px([.35,-.5])-[1485,1457]),5)
                self.assertAlmostEqual(reg.transform.px_per_mm,7.75,delta=.05)
                turned = np.rot90(scan.pixels)[:,::-1]
                flipped = registration.register(turned,load_default(),spacing_mm=.139)
                self.assertTrue(flipped.transform.mirrored)
                expected = np.array([scan.shape[0]-1-1457,scan.shape[1]-1-1485])
                self.assertLess(np.linalg.norm(flipped.transform.mm_to_px([.35,-.5])-expected),5)
            else:
                truth = np.array([[90,310],[2420,281],[2454,2614],[120,2648]])
                for point in truth:
                    self.assertLess(np.min(np.linalg.norm(reg.corners_px-point,axis=1)),12)


if __name__ == '__main__':
    unittest.main()
