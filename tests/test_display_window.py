"""Image preview W/C shares its automatic limits with the viewer sliders."""

import unittest

import numpy as np

from phantom_qa.webapp.display import display_range, window_image


class DisplayWindowTests(unittest.TestCase):
    def test_auto_and_default_slider_window_are_identical_across_bit_depths(self):
        for maximum in (4095, 16383, 0.02):
            with self.subTest(maximum=maximum):
                pixels = np.linspace(0, maximum, 10000).reshape(100, 100)
                r = display_range(pixels)
                auto = window_image(pixels)
                manual = window_image(pixels, (r["hi"] + r["lo"]) / 2,
                                      r["hi"] - r["lo"])
                np.testing.assert_array_equal(auto, manual)
                self.assertEqual(auto[0, 0], 0)
                self.assertEqual(auto[-1, -1], 255)

    def test_width_changes_contrast_and_centre_changes_brightness(self):
        pixels = np.array([[0, 25, 50, 75, 100]], dtype=float)
        np.testing.assert_array_equal(window_image(pixels, 50, 100),
                                      [[0, 63, 127, 191, 255]])
        np.testing.assert_array_equal(window_image(pixels, 50, 50),
                                      [[0, 0, 127, 255, 255]])
        np.testing.assert_array_equal(window_image(pixels, 75, 100),
                                      [[0, 0, 63, 127, 191]])

    def test_fractional_window_values_are_not_rounded_away(self):
        pixels = np.array([[0.008, 0.009, 0.010, 0.011, 0.012]])
        np.testing.assert_array_equal(window_image(pixels, 0.010, 0.004),
                                      [[0, 63, 127, 191, 255]])

    def test_flat_images_have_a_finite_nonzero_window(self):
        for value in (0, 100, -40):
            with self.subTest(value=value):
                pixels = np.full((10, 10), value)
                r = display_range(pixels)
                self.assertGreater(r["hi"], r["lo"])
                self.assertEqual((r["hi"] + r["lo"]) / 2, value)
                np.testing.assert_array_equal(window_image(pixels),
                                              np.full_like(pixels, 127))

    def test_invalid_windows_fall_back_to_auto(self):
        pixels = np.arange(100).reshape(10, 10)
        for wc, ww in ((None, None), (50, 0), (50, -1),
                       (float("nan"), 100), (50, float("inf"))):
            np.testing.assert_array_equal(window_image(pixels, wc, ww),
                                          window_image(pixels))

    def test_rendering_does_not_modify_source_pixels(self):
        pixels = np.arange(100, dtype=np.uint16).reshape(10, 10)
        before = pixels.copy()
        window_image(pixels, 50, 80)
        np.testing.assert_array_equal(pixels, before)


if __name__ == "__main__":
    unittest.main()
