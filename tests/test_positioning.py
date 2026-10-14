"""Known-pose synthetic checks; runnable with unittest as well as pytest."""

from __future__ import annotations

import unittest
import copy
import json
from unittest.mock import patch

import numpy as np

from phantom_qa import layout_profile, pipeline
from phantom_qa.analysis import linepairs, lowcontrast, uniformity, wedge
from phantom_qa.analysis.common import Ctx, roi_translate_mm
from phantom_qa.analysis.positioning import (OutlineMatcher, build_templates, project,
                                            rectangle_outline, refine_pose, rotation)
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform


def synthetic_ctx(global_angle=0.0, mirrored=False, circle_signal=True, pitch_scale=1.0):
    """Render known displaced/rotated objects, including weak/absent discs."""
    p = load_default()
    ppm, n = 4.0, 1500
    T = Transform(rotation(global_angle) @ np.diag([ppm, ppm if mirrored else -ppm]),
                  np.array([n / 2, n / 2]))
    yy, xx = np.indices((n, n), dtype=float)
    mm = T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(n, n, 2)
    image = np.full((n, n), 2400.0)
    truth = {}

    for sq in p.uniformity["squares"]:
        center = np.array(sq["center_mm"]) + [-3.25, 2.5]
        angle = 3.0
        local = (mm - center) @ rotation(angle)
        half = p.uniformity["size_mm"] / 2
        ridge = ((np.abs(np.abs(local[:, :, 0]) - half) < 0.35) &
                 (np.abs(local[:, :, 1]) <= half))
        ridge |= ((np.abs(np.abs(local[:, :, 1]) - half) < 0.35) &
                  (np.abs(local[:, :, 0]) <= half))
        image[ridge] += 700
        truth["uniformity/" + sq["id"]] = center

    lc = p.lowcontrast
    center = np.array(lc["center_mm"]) + [5.25, -4.0]
    angle = lc["angle_deg"] + 6.0
    local = (mm - center) @ rotation(angle)
    inside = np.all(np.abs(local) <= np.array(lc["size_mm"]) / 2, axis=2)
    image[inside] = 3300
    grid_shift = np.array([0.75, -0.5])
    for i, circle in enumerate(lc["circles"]):
        cc = np.array([circle["u_mm"], circle["v_mm"]]) + grid_shift
        disc = np.sum((local - cc) ** 2, axis=2) <= (lc["circle_dia_mm"] / 2) ** 2
        image[disc] -= 0 if i < 3 or not circle_signal else 60 * (i - 2)
    truth["lowcontrast"] = (center, angle, grid_shift)

    lp = p.linepairs
    centers = np.array([g["center_mm"] for g in lp["groups"]])
    origin = centers.mean(axis=0)
    angle = lp["strip_angle_deg"] + 5.0
    displaced = project((centers - origin) @ rotation(lp["strip_angle_deg"]),
                        origin + [8.0, -6.0], angle)
    for g, center in zip(lp["groups"], displaced):
        local = (mm - center) @ rotation(angle)
        inside = np.all(np.abs(local) <= lp["roi_size_mm"] / 2, axis=2)
        image += np.where(inside, 500 * np.cos(2 * np.pi * g["freq_lp_mm"] * pitch_scale * local[:, :, 1]), 0)
        truth["linepairs/" + g["id"]] = center

    w = p.wedge
    half_width = w["frame_width_mm"] / 2
    cy = (w["y_top_mm"] + w["y_bottom_mm"]) / 2
    center = np.array([w["center_x_mm"] + 3, cy - 4])
    angle = 3.0
    local = (mm - center) @ rotation(angle)
    bounds = np.array([w["y_top_mm"], *w["nominal_boundaries_y_mm"], w["y_bottom_mm"]]) - cy
    means = [3545, 3448, 3288, 2881, 2327, 1753, 855]
    for i, mean in enumerate(means):
        inside = ((np.abs(local[:, :, 0]) <= half_width) &
                  (local[:, :, 1] <= bounds[i]) & (local[:, :, 1] > bounds[i + 1]))
        image[inside] = mean
    frame = ((np.abs(np.abs(local[:, :, 0]) - half_width) < 0.3) &
             (local[:, :, 1] <= bounds[0]) & (local[:, :, 1] >= bounds[-1]))
    image[frame] = 500
    truth["wedge"] = (center, angle, bounds)
    image += np.random.default_rng(5).normal(0, 10, image.shape)
    return Ctx(image, T, p), truth


class PoseSearchTests(unittest.TestCase):
    def test_recovers_bounded_pose_and_improves_score(self):
        def score(center, angle, level):
            return -float(np.sum((center - [5.25, -3.5]) ** 2) + (angle - 6.25) ** 2)
        result = refine_pose(score, [0, 0], search_mm=10, angle_limit_deg=10)
        np.testing.assert_allclose(result["center_mm"], [5.25, -3.5], atol=0.25)
        self.assertAlmostEqual(result["angle_deg"], 6.25, delta=0.25)
        self.assertGreater(result["score"], result["initial_score"])
        self.assertEqual(len(result["levels"]), 4)

    def test_search_cannot_escape_its_mask(self):
        result = refine_pose(lambda c, a, level: c[0] + a, [0, 0],
                             search_mm=3, angle_limit_deg=2)
        self.assertLessEqual(np.linalg.norm(result["center_mm"]), 3.0)
        self.assertLessEqual(abs(result["angle_deg"]), 2.0)
        self.assertTrue(result["at_bound"])

    def test_flat_and_clipped_images_retain_the_template(self):
        for n, ppm in [(1300, 4.0), (32, 1.0)]:
            ctx = Ctx(np.full((n, n), 2400.0),
                      Transform(np.diag([ppm, -ppm]), np.array([n / 2, n / 2])), load_default())
            template = build_templates(ctx)["uniformity/TL"]
            matcher = OutlineMatcher(ctx, template, *rectangle_outline(template["size_mm"]))
            result = matcher.fit(template, required_sections=4)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["center_mm"], template["center_mm"])
            json.dumps(result, allow_nan=False)

    def test_clipped_proposals_are_safe_to_return_as_json(self):
        ctx = Ctx(np.full((32, 32), 2400.0), Transform(np.diag([1.0, -1.0]),
                  np.array([16.0, 16.0])), load_default())
        result = pipeline.propose_all(ctx)
        json.dumps(result, allow_nan=False)
        self.assertFalse(result["lowcontrast"]["detected"])
        self.assertFalse(result["wedge"]["detected"])

    def test_incomplete_reversed_strip_preserves_group_identity(self):
        ctx = Ctx(np.ones((2, 2)), Transform(np.eye(2), np.zeros(2)), load_default())
        blocks = [{"center_mm": [i * 20, i * 20], "freq_lp_mm": f}
                  for i, f in enumerate([1.144, 1.287, 1.43, 1.647])]
        result = linepairs._match_blocks_to_groups(ctx, blocks, 45.0)
        self.assertIs(result["G1.1"], blocks[0])
        self.assertIs(result["G1.2"], blocks[1])
        self.assertNotIn("G2.0", result)

    def test_one_invalid_feature_definition_does_not_block_other_proposals(self):
        pdef = load_default()
        pdef.wedge = {}
        ctx = Ctx(np.full((32, 32), 2400.0), Transform(np.diag([1.0, -1.0]),
                  np.array([16.0, 16.0])), pdef)
        result = pipeline.propose_all(ctx)
        self.assertIsNotNone(result["wedge"]["_error"])
        for name in ("geometry", "uniformity", "lowcontrast", "linepairs"):
            self.assertIsNone(result[name]["_error"], result[name])


class FeaturePositioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx, cls.truth = synthetic_ctx()

    def test_uniformity_centres_and_measured_sizes(self):
        result = uniformity.propose(self.ctx)
        for sq in result["squares"]:
            self.assertTrue(sq["detected"], sq["positioning"])
            np.testing.assert_allclose(sq["roi"]["center_mm"], self.truth["uniformity/" + sq["id"]], atol=0.4)
            self.assertAlmostEqual(sq["outline"]["measured_w_mm"], self.ctx.pdef.uniformity["size_mm"], delta=0.4)
            self.assertEqual(sq["roi"]["size_mm"], [30.0, 30.0])

    def test_lowcontrast_rigid_grid_including_invisible_circles(self):
        result = lowcontrast.propose(self.ctx)
        center, angle, shift = self.truth["lowcontrast"]
        self.assertTrue(result["detected"], result["positioning"])
        np.testing.assert_allclose(result["block"]["center_mm"], center, atol=0.4)
        self.assertAlmostEqual(result["angle_deg"], angle, delta=0.5)
        np.testing.assert_allclose(result["grid_shift_mm"], shift, atol=0.5)
        for circle, definition in zip(result["circles"], self.ctx.pdef.lowcontrast["circles"]):
            expected = project([[definition["u_mm"], definition["v_mm"]] + shift], center, angle)[0]
            np.testing.assert_allclose(circle["roi"]["center_mm"], expected, atol=0.6)
            self.assertEqual(circle["roi"]["center_mm"], circle["bg_roi"]["center_mm"])

    def test_linepairs_follow_shift_and_rotation_without_forcing_pitch(self):
        result = linepairs.propose(self.ctx)
        for group in result["groups"]:
            self.assertTrue(group["detected"], group)
            np.testing.assert_allclose(group["roi"]["center_mm"], self.truth["linepairs/" + group["id"]], atol=1.5)
        measured = linepairs.compute(self.ctx, result)
        for row in measured["rows"]:
            pitch = row["linearity"].get("measured_pitch_mm")
            self.assertIsNotNone(pitch)
            self.assertAlmostEqual(pitch, 1 / row["freq_lp_mm"], delta=0.03)

    def test_wedge_group_rotation_and_step_boundaries(self):
        result = wedge.propose(self.ctx)
        center, angle, bounds = self.truth["wedge"]
        self.assertTrue(result["detected"], result["positioning"])
        self.assertAlmostEqual(result["angle_deg"], angle, delta=0.5)
        for i, step in enumerate(result["steps"]):
            expected = project([[0, (bounds[i] + bounds[i + 1]) / 2]], center, angle)[0]
            np.testing.assert_allclose(step["roi"]["center_mm"], expected, atol=0.7)
            self.assertEqual(step["roi"]["size_mm"], [10.0, 10.0])

    def test_global_rotation_and_mirror_are_handled_by_registration_transform(self):
        ctx, truth = synthetic_ctx(global_angle=27, mirrored=True)
        result = lowcontrast.propose(ctx)
        self.assertTrue(result["detected"], result["positioning"])
        np.testing.assert_allclose(result["block"]["center_mm"], truth["lowcontrast"][0], atol=0.5)
        np.testing.assert_allclose(result["block"]["center_px"],
                                   ctx.T.mm_to_px(truth["lowcontrast"][0]), atol=2.0)
        for group in linepairs.propose(ctx)["groups"]:
            self.assertTrue(group["detected"], group)
            np.testing.assert_allclose(group["roi"]["center_mm"],
                                       truth["linepairs/" + group["id"]], atol=1.5)
        result = wedge.propose(ctx)
        self.assertTrue(result["detected"], result["positioning"])
        self.assertAlmostEqual(result["angle_deg"], truth["wedge"][1], delta=0.5)

    def test_noise_without_circles_does_not_move_the_grid(self):
        ctx, _ = synthetic_ctx(circle_signal=False)
        result = lowcontrast.propose(ctx)
        self.assertTrue(result["detected"])
        self.assertFalse(result["grid_positioning"]["accepted"])
        self.assertEqual(result["grid_shift_mm"], [0.0, 0.0])

    def test_actual_pitch_error_survives_position_optimisation(self):
        ctx, _ = synthetic_ctx(pitch_scale=1.04)
        result = linepairs.compute(ctx, linepairs.propose(ctx))
        for row in result["rows"]:
            expected = 1 / (1.04 * row["freq_lp_mm"])
            self.assertAlmostEqual(row["linearity"]["measured_pitch_mm"], expected, delta=0.012)

    def test_compute_respects_manual_edits_without_running_position_search(self):
        result = linepairs.propose(self.ctx)
        group = result["groups"][0]
        group["roi"] = roi_translate_mm(self.ctx, group["roi"], 0.5, -0.25)
        group["profile_seg"] = linepairs.profile_for_center(
            self.ctx, group["roi"]["center_mm"], result["roi_size_mm"], group["profile_seg"]["id"])
        group["profile_seg"]["manually_adjusted"] = True
        saved = copy.deepcopy(result)
        with patch('phantom_qa.analysis.linepairs.refine_pose', side_effect=AssertionError):
            linepairs.compute(self.ctx, result)
        self.assertEqual(result, saved)

    def test_saved_layout_keeps_rotated_wedge_and_refreshes_pixel_coordinates(self):
        geometry = {"wedge": wedge.propose(self.ctx)}
        layout = layout_profile.extract_layout(geometry)
        ctx = Ctx(self.ctx.pixels, Transform(rotation(23) @ self.ctx.T.A, self.ctx.T.t), self.ctx.pdef)
        out, report = layout_profile.apply_layout(ctx, copy.deepcopy(geometry), layout)
        self.assertEqual(report["n_applied"], 8)
        self.assertEqual(out["wedge"]["angle_deg"], geometry["wedge"]["angle_deg"])
        for step in out["wedge"]["steps"]:
            np.testing.assert_allclose(step["roi"]["center_px"], ctx.T.mm_to_px(step["roi"]["center_mm"]))


if __name__ == "__main__":
    unittest.main()
