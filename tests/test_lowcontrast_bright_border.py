"""Low-contrast rectangles follow bright border crests, including offsets."""

import unittest
from unittest.mock import patch

import numpy as np
from scipy import ndimage

from phantom_qa.analysis import lowcontrast
from phantom_qa.analysis.common import Ctx
from phantom_qa.analysis.positioning import rotation
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform


def bright_border_scan(border=True, global_angle=0.0, mirrored=False, flat_border=False):
    pdef = load_default()
    lc = pdef.lowcontrast
    ppm, n = 4.0, 800
    A = rotation(global_angle) @ np.diag([ppm, ppm if mirrored else -ppm])
    T = Transform(A, np.array([n / 2, n / 2]) - A @ lc["center_mm"])
    yy, xx = np.indices((n, n), dtype=float)
    mm = T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(n, n, 2)
    body_center = np.array(lc["center_mm"]) + [4, -3]
    angle = lc["angle_deg"] + 4
    body = (mm - body_center) @ rotation(angle)
    half = np.array(lc["size_mm"]) / 2
    image = np.where(np.all(np.abs(body) <= half, axis=2), 3000.0, 2400.0)
    image = ndimage.gaussian_filter(image, 0.5 * ppm)
    # The bright contour is offset from the body's geometric centre. The
    # rectangle must follow this contour rather than recentre on the body.
    crest_center = body_center + rotation(angle) @ [1.25, -0.75]
    if border:
        local = (mm - crest_center) @ rotation(angle)
        vertical = np.exp(-0.5 * ((np.abs(local[:, :, 0]) - half[0]) / 1.0) ** 2)
        horizontal = np.exp(-0.5 * ((np.abs(local[:, :, 1]) - half[1]) / 1.0) ** 2)
        vertical *= np.abs(local[:, :, 1]) <= half[1]
        horizontal *= np.abs(local[:, :, 0]) <= half[0]
        if flat_border:
            band = ((np.abs(np.abs(local[:, :, 0]) - half[0]) < 1.5) &
                    (np.abs(local[:, :, 1]) <= half[1]))
            band |= ((np.abs(np.abs(local[:, :, 1]) - half[1]) < 1.5) &
                     (np.abs(local[:, :, 0]) <= half[0]))
            image[band] = 5000
            image = ndimage.gaussian_filter(image, 0.2 * ppm)
        else:
            image += 2500 * np.maximum(vertical, horizontal)
    for i, circle in enumerate(lc["circles"]):
        disc = np.sum((body - [circle["u_mm"], circle["v_mm"]]) ** 2, axis=2) < 25
        image[disc] -= 0 if i < 3 else 60 * (i - 2)
    image += np.random.default_rng(7).normal(0, 10, image.shape)
    return Ctx(image, T, pdef), body_center, crest_center, angle


class BrightBorderTests(unittest.TestCase):
    def test_refinement_places_outline_on_offset_bright_border(self):
        ctx, body, crest, angle = bright_border_scan()
        coarse_center, coarse_angle, _ = lowcontrast._refine_block(ctx)
        border = lowcontrast._refine_bright_border(ctx, coarse_center, coarse_angle)
        center, fitted_angle = border["center_mm"], border["angle_deg"]
        self.assertTrue(border["accepted"], border)
        np.testing.assert_allclose(center, crest, atol=0.35)
        self.assertGreater(np.linalg.norm(np.array(center) - body), 1.0)
        self.assertAlmostEqual(fitted_angle, angle, delta=0.3)
        self.assertGreater(border["score"], border["initial_score"])
        self.assertEqual(border["score_type"], "perimeter_brightness_sum")

    def test_circle_refinement_preserves_the_bright_outline(self):
        ctx, _, crest, _ = bright_border_scan()
        with patch.object(lowcontrast, "_refine_bright_border", return_value={"accepted": False}):
            before = lowcontrast.propose(ctx)
        result = lowcontrast.propose(ctx)
        self.assertTrue(result["positioning"]["bright_border"]["accepted"])
        np.testing.assert_allclose(result["block"]["center_mm"], crest, atol=0.35)
        self.assertEqual(result["block"]["angle_deg"],
                         result["positioning"]["bright_border"]["angle_deg"])
        self.assertEqual(result["block"]["size_mm"], ctx.pdef.lowcontrast["size_mm"])
        self.assertEqual(result["circles"], before["circles"])
        self.assertEqual(lowcontrast.compute(ctx, result), lowcontrast.compute(ctx, before))
        for circle in result["circles"]:
            self.assertEqual(circle["roi"]["center_mm"], circle["bg_roi"]["center_mm"])
        rebuilt = lowcontrast.circles_for_block(
            ctx, result["block"]["center_mm"], result["angle_deg"], result["grid_shift_mm"],
            result["grid_angle_offset_deg"])
        for saved, circle in zip(result["circles"], rebuilt):
            np.testing.assert_allclose(saved["roi"]["center_mm"], circle["roi"]["center_mm"])

    def test_flat_topped_thick_border_stays_near_its_middle(self):
        ctx, _, crest, _ = bright_border_scan(flat_border=True)
        center, angle, _ = lowcontrast._refine_block(ctx)
        border = lowcontrast._refine_bright_border(ctx, center, angle)
        self.assertTrue(border["accepted"], border)
        np.testing.assert_allclose(border["center_mm"], crest, atol=0.4)

    def test_plain_bright_body_retains_coarse_placement(self):
        ctx, _, _, _ = bright_border_scan(border=False)
        center, angle, diagnostics = lowcontrast._refine_block(ctx)
        self.assertTrue(diagnostics["accepted"])
        border = lowcontrast._refine_bright_border(ctx, center, angle)
        self.assertFalse(border["accepted"])
        self.assertEqual(center, border["center_mm"])
        self.assertEqual(angle, border["angle_deg"])

    def test_rotated_mirrored_image_still_follows_bright_border(self):
        ctx, _, crest, angle = bright_border_scan(global_angle=23, mirrored=True)
        coarse_center, coarse_angle, _ = lowcontrast._refine_block(ctx)
        border = lowcontrast._refine_bright_border(ctx, coarse_center, coarse_angle)
        center, fitted_angle = border["center_mm"], border["angle_deg"]
        self.assertTrue(border["accepted"], border)
        np.testing.assert_allclose(center, crest, atol=0.35)
        self.assertAlmostEqual(fitted_angle, angle, delta=0.3)

    def test_clipped_image_has_no_accepted_border(self):
        ctx, _, _, _ = bright_border_scan()
        ctx.pixels = ctx.pixels[:100, :100]
        _, _, diagnostics = lowcontrast._refine_block(ctx)
        self.assertFalse(diagnostics["accepted"])
        self.assertNotIn("bright_border", diagnostics)

    def test_saved_layout_keeps_grid_offset_relative_to_border(self):
        from phantom_qa.layout_profile import extract_layout
        ctx, _, _, _ = bright_border_scan()
        result = lowcontrast.propose(ctx)
        scalars = extract_layout({"lowcontrast": result})["scalars"]["lowcontrast"]
        self.assertEqual(scalars["grid_angle_offset_deg"], result["grid_angle_offset_deg"])
        self.assertEqual(scalars["grid_shift_mm"], result["grid_shift_mm"])

    def test_legacy_layout_restores_a_shared_outline_and_grid_angle(self):
        from phantom_qa.layout_profile import extract_layout, apply_layout
        ctx, _, _, _ = bright_border_scan()
        with patch.object(lowcontrast, "_refine_bright_border", return_value={"accepted": False}):
            previous = lowcontrast.propose(ctx)
        layout = extract_layout({"lowcontrast": previous})
        del layout["scalars"]["lowcontrast"]["grid_angle_offset_deg"]
        fresh = lowcontrast.propose(ctx)
        result, _ = apply_layout(ctx, {"lowcontrast": fresh}, layout)
        restored = result["lowcontrast"]
        self.assertEqual(restored["grid_angle_offset_deg"], 0.0)
        rebuilt = lowcontrast.circles_for_block(
            ctx, restored["block"]["center_mm"], restored["angle_deg"], restored["grid_shift_mm"])
        for saved, circle in zip(restored["circles"], rebuilt):
            np.testing.assert_allclose(saved["roi"]["center_mm"], circle["roi"]["center_mm"])


if __name__ == "__main__":
    unittest.main()
