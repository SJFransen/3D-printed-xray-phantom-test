"""Final connector alignment, including safe fallback without a middle line."""

import copy
import unittest

import numpy as np

from phantom_qa.analysis import linepairs
from phantom_qa.analysis.common import Ctx
from phantom_qa.analysis.positioning import rotation
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform


def connector_scan(connector=True, broken_gap=False):
    ppm, n = 4.0, 700
    transform = Transform(np.diag([ppm, -ppm]), np.array([n / 2, n / 2]))
    yy, xx = np.indices((n, n), dtype=float)
    mm = transform.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(n, n, 2)
    true_angle, initial_angle = 38.0, 36.0
    local = mm @ rotation(true_angle)
    along, across = local[:, :, 0], local[:, :, 1]
    slots = np.array([-60, -30, 0, 30, 60])
    in_grating = np.min(np.abs(along[:, :, None] - slots), axis=2) < 9
    image = np.full((n, n), 2400.0)
    image += np.where(in_grating & (np.abs(across) < 9),
                      400 * np.cos(2 * np.pi * 1.2 * across), 0)
    # Stronger outer frame lines must never become the middle-line reference.
    image += 1000 * np.exp(-0.5 * ((np.abs(across) - 10) / 0.18) ** 2)
    if connector:
        visible = ~in_grating
        if broken_gap:
            visible &= ~((along > -50) & (along < -40))
        image += np.where(visible, 500 * np.exp(-0.5 * ((across - 1) / 0.18) ** 2), 0)
    image += np.random.default_rng(4).normal(0, 10, image.shape)
    seed = np.c_[slots, [-0.8, 0.5, -0.3, 0.8, -0.2]] @ rotation(initial_angle).T
    blocks = [{"center_mm": c.tolist(), "positioning": {}, "freq_lp_mm": 1.2,
               "mod_dir_deg": 126.0, "t": float(t)} for c, t in zip(seed, slots)]
    return Ctx(image, transform, load_default()), blocks, initial_angle, true_angle


class MiddleLineTests(unittest.TestCase):
    def test_centres_follow_connector_and_keep_along_line_positions(self):
        ctx, blocks, initial_angle, true_angle = connector_scan()
        before = copy.deepcopy(blocks)
        aligned, angle = linepairs._align_blocks_to_middle_line(ctx, blocks, initial_angle)
        self.assertAlmostEqual(angle, true_angle, delta=0.2)
        actual = np.array([b["center_mm"] for b in aligned]) @ rotation(true_angle)
        np.testing.assert_allclose(actual[:, 1], 1.0, atol=0.12)
        old_along = np.array([b["center_mm"] for b in before]) @ rotation(angle)[:, 0]
        new_along = np.array([b["center_mm"] for b in aligned]) @ rotation(angle)[:, 0]
        np.testing.assert_allclose(new_along, old_along, atol=1e-9)
        for old, new in zip(before, aligned):
            self.assertEqual(old["freq_lp_mm"], new["freq_lp_mm"])
            self.assertEqual(old["mod_dir_deg"], new["mod_dir_deg"])
            self.assertIn("middle_line", new["positioning"])

    def test_one_missing_connector_gap_still_aligns(self):
        ctx, blocks, angle, truth = connector_scan(broken_gap=True)
        aligned, fitted = linepairs._align_blocks_to_middle_line(ctx, blocks, angle)
        self.assertAlmostEqual(fitted, truth, delta=0.2)
        self.assertIn("middle_line", aligned[0]["positioning"])

    def test_no_connector_retains_current_positions_and_angle(self):
        ctx, blocks, angle, _ = connector_scan(connector=False)
        saved = copy.deepcopy(blocks)
        result, fitted = linepairs._align_blocks_to_middle_line(ctx, blocks, angle)
        self.assertEqual(result, saved)
        self.assertEqual(fitted, angle)

    def test_clipped_connector_retains_current_positions(self):
        ctx, blocks, angle, _ = connector_scan()
        ctx.pixels = ctx.pixels[:100, :100]
        saved = copy.deepcopy(blocks)
        result, fitted = linepairs._align_blocks_to_middle_line(ctx, blocks, angle)
        self.assertEqual(result, saved)
        self.assertEqual(fitted, angle)

    def test_proposal_centres_and_roi_angles_use_final_middle_line(self):
        from test_positioning import synthetic_ctx
        ctx, truth = synthetic_ctx()
        angle = ctx.pdef.linepairs["strip_angle_deg"] + 5
        centers = np.array([truth["linepairs/" + g["id"]] for g in ctx.pdef.linepairs["groups"]])
        origin = centers.mean(axis=0)
        yy, xx = np.indices(ctx.pixels.shape, dtype=float)
        mm = ctx.T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(*ctx.pixels.shape, 2)
        local = (mm - origin) @ rotation(angle)
        slots = (centers - origin) @ rotation(angle)[:, 0]
        gap = np.min(np.abs(local[:, :, 0, None] - slots), axis=2) > 9
        ctx.pixels += np.where(gap, 600 * np.exp(-0.5 * ((local[:, :, 1] - 0.9) / 0.18) ** 2), 0)
        proposal = linepairs.propose(ctx)
        for group in proposal["groups"]:
            self.assertTrue(group["detected"], group)
            self.assertIn("middle_line", group["positioning"])
            transverse = (np.array(group["roi"]["center_mm"]) - origin) @ rotation(angle)[:, 1]
            self.assertAlmostEqual(transverse, 0.9, delta=0.15)
            self.assertAlmostEqual((group["roi"]["angle_deg"] - proposal["strip_angle_deg"]) % 180,
                                   45.0, delta=1e-9)
            profile = group["profile_seg"]
            np.testing.assert_allclose((np.array(profile["p0_mm"]) + profile["p1_mm"]) / 2,
                                       group["roi"]["center_mm"])


if __name__ == "__main__":
    unittest.main()
