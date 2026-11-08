"""Motion-blend smoothing for Shorts: which source frames each output frame averages."""
from __future__ import annotations

import unittest

import numpy as np

from reels_ai import fx
from reels_ai.variants import _mix, blend_windows


class BlendWindowTest(unittest.TestCase):
    def test_speedup_blends_skipped_frames(self):
        w = blend_windows([0, 4, 8, 12], blend=6, min_win=3)
        self.assertEqual(w[0], (0,))
        self.assertEqual(w[1], (1, 2, 3, 4))
        self.assertEqual(w[3], (9, 10, 11, 12))

    def test_real_speed_keeps_short_trail_and_caps(self):
        w = blend_windows([10, 11, 30], blend=6, min_win=3)
        self.assertEqual(w[1], (9, 10, 11))
        self.assertEqual(w[2], (25, 26, 27, 28, 29, 30))         # capped at 6 frames

    def test_holds_flashbacks_and_cuts_stay_sharp(self):
        w = blend_windows([50, 50, 5, 200], blend=6)
        self.assertEqual(w[1:], [(50,), (5,), (200,)])

    def test_arm_frames_left_out(self):
        usable = [True] * 20
        usable[6] = usable[7] = False
        self.assertEqual(blend_windows([4, 9], usable, blend=6)[1], (3, 4, 5, 8, 9))

    def test_mix_weights_newest_most(self):
        a, b = np.zeros((4, 4, 3), np.uint8), np.full((4, 4, 3), 90, np.uint8)
        self.assertEqual(int(_mix([a, b])[0, 0, 0]), 60)          # (0*1 + 90*2) / 3

    def test_combined_camera_matches_two_steps(self):
        img = (np.random.default_rng(1).random((320, 180, 3)) * 255).astype(np.uint8)
        self.assertTrue(np.array_equal(fx.camera(img, 0.5, 0.5, 1.0, (0.3, 0.6, 1.4)),
                                       fx.camera(img, 0.3, 0.6, 1.4)))
        self.assertIs(fx.camera(img, 0.5, 0.5, 1.0, (0.5, 0.5, 1.0)), img)


if __name__ == "__main__":
    unittest.main()
