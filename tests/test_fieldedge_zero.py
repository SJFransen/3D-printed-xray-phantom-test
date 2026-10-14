"""Field markers follow the blue ruler rays to actual zero-valued pixels."""

import unittest
from unittest.mock import patch

import numpy as np

from phantom_qa.analysis import geometry
from phantom_qa.analysis.common import Ctx, segment
from phantom_qa.analysis.positioning import rotation
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform


def zero_field(offset=15.0, angle=0.0, mirrored=False, margin=35.0):
    pdef = load_default()
    ppm = 2.0
    n = int((pdef.side_mm + 2 * margin) * ppm)
    T = Transform(rotation(angle) @ np.diag([ppm, ppm if mirrored else -ppm]),
                  np.array([n // 2, n // 2], float))
    yy, xx = np.indices((n, n), dtype=float)
    mm = T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(n, n, 2)
    radius = np.max(np.abs(mm), axis=2)
    image = np.where(radius < pdef.side_mm / 2 + offset, 250.0, 0.0)
    image[radius < min(pdef.side_mm / 2, pdef.side_mm / 2 + offset)] = 2400.0
    return Ctx(image, T, pdef)


class ZeroFieldEdgeTests(unittest.TestCase):
    def check_sides(self, ctx, offset):
        for side in geometry.SIDES:
            with self.subTest(side=side):
                field = geometry._field_edge(ctx, side)
                self.assertTrue(field["detected"], field)
                self.assertEqual(field["method"], "first_zero")
                self.assertAlmostEqual(field["offset_from_edge_mm"], offset, delta=0.75)
                px = np.rint(field["edge_pt_px"]).astype(int)
                self.assertEqual(ctx.pixels[px[1], px[0]], 0)
                (_, _), inward = geometry._SIDE_GEOM[side]
                mm = ctx.T.px_to_mm(field["edge_pt_px"])
                tangent = np.array([-inward[1], inward[0]])
                self.assertAlmostEqual(np.dot(mm, tangent), 0, delta=1e-8)

    def test_all_four_markers_outside_phantom(self):
        self.check_sides(zero_field(), 15)

    def test_all_four_markers_inside_phantom(self):
        self.check_sides(zero_field(offset=-18), -18)

    def test_boundary_inside_inner_end_of_blue_probe(self):
        self.check_sides(zero_field(offset=-55), -55)

    def test_rotated_and_mirrored_phantom(self):
        for mirrored in (False, True):
            with self.subTest(mirrored=mirrored):
                self.check_sides(zero_field(angle=27, mirrored=mirrored, margin=100), 15)

    def test_edge_near_detector_border_needs_no_external_margin(self):
        self.check_sides(zero_field(offset=8, margin=10), 8)

    def test_real_zero_pixels_at_last_image_column_are_detected(self):
        ctx = zero_field()
        ctx.pixels[:] = 2400
        ctx.pixels[:, -1] = 0
        field = geometry._field_edge(ctx, "right")
        self.assertTrue(field["detected"], field)
        self.assertEqual(field["method"], "first_zero")
        self.assertEqual(field["edge_pt_px"][0], ctx.pixels.shape[1] - 1)

    def test_walk_uses_actual_blue_probe_line(self):
        ctx = zero_field()
        half = ctx.pdef.side_mm / 2
        ruler = {"probe": segment(ctx, [7, half + 2], [7, half - 42])}
        field = geometry._field_edge(ctx, "top", ruler=ruler)
        self.assertTrue(field["detected"])
        self.assertAlmostEqual(ctx.T.px_to_mm(field["edge_pt_px"])[0], 7)

    def test_isolated_zero_does_not_replace_field_boundary(self):
        ctx = zero_field()
        point = np.rint(ctx.T.mm_to_px([0, ctx.pdef.side_mm / 2 - 10])).astype(int)
        ctx.pixels[point[1], point[0]] = 0
        self.check_sides(ctx, 15)

    def test_no_zero_pixels_keeps_existing_step_detector(self):
        ctx = zero_field(offset=40, margin=90)
        ctx.pixels[ctx.pixels == 0] = 3900
        for side in geometry.SIDES:
            field = geometry._field_edge(ctx, side)
            self.assertTrue(field["detected"], field)
            self.assertAlmostEqual(field["offset_from_edge_mm"], 40, delta=2)

    def test_no_actual_zero_boundary_is_not_invented_at_image_border(self):
        ctx = zero_field()
        ctx.pixels[:] = 2400
        for side in geometry.SIDES:
            self.assertFalse(geometry._field_edge(ctx, side)["detected"])

    def test_entirely_zero_scan_has_no_exposed_field(self):
        ctx = zero_field()
        ctx.pixels[:] = 0
        for side in geometry.SIDES:
            self.assertFalse(geometry._field_edge(ctx, side)["detected"])

    def test_patterns_proposal_contains_all_four_automatic_markers(self):
        ctx = zero_field(offset=-18)
        with patch.object(geometry, "_ruler_probe", wraps=geometry._ruler_probe) as rulers:
            proposal = geometry.propose(ctx)
        self.assertEqual(rulers.call_count, 4)
        self.assertEqual(set(proposal["field_edges"]), set(geometry.SIDES))
        for field in proposal["field_edges"].values():
            self.assertTrue(field["detected"])
            self.assertFalse(field["manual"])
            self.assertLess(field["offset_from_edge_mm"], 0)


if __name__ == "__main__":
    unittest.main()
