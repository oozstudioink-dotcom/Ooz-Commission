"""Smooth motion for Shorts: speed ramps never repeat a frame, blends replace jumps."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from reels_ai import fx
from reels_ai.variants import MAX_SLOW, blend_windows, mix, ramp


def _an(n, usable=None):
    usable = usable or [True] * n
    return SimpleNamespace(n_frames=n, usable=usable,
                           clean_range=lambda a, b: [i for i in range(a, b + 1) if usable[i]] or list(range(a, b + 1)))


def frames(win):
    return [i for i, _ in win]


class RampTest(unittest.TestCase):
    def test_sparse_clip_never_repeats_a_frame(self):
        # 100 source frames asked to fill 8 s (240 frames): slows down by cross-fading.
        pos = ramp(_an(200), 50, 150, 8.0, focus=100)
        self.assertEqual(len(pos), int(MAX_SLOW * 100) + 1)          # as long as it can go smoothly
        self.assertTrue(all(b > a for a, b in zip(pos, pos[1:])))   # always moving forward
        wins = blend_windows(pos)
        self.assertTrue(all(w1 != w2 for w1, w2 in zip(wins, wins[1:])))  # no frozen frames

    def test_speed_ramp_slows_at_focus(self):
        pos = np.array(ramp(_an(1000), 0, 999, 6.0, focus=500))
        steps = np.diff(pos)
        near = steps[np.abs(pos[1:] - 500) < 30].mean()
        far = steps[np.abs(pos[1:] - 500) > 300].mean()
        self.assertLess(near, far / 2)
        self.assertAlmostEqual(pos[0], 0)
        self.assertAlmostEqual(pos[-1], 999)

    def test_arm_frames_stepped_over(self):
        usable = [True] * 100
        for i in range(40, 60):
            usable[i] = False
        wins = blend_windows(ramp(_an(100, usable), 0, 99, 3.0), usable)
        self.assertFalse({i for w in wins for i in frames(w)} & set(range(40, 60)))


class BlendWindowTest(unittest.TestCase):
    def test_speedup_blends_skipped_frames_newest_most(self):
        w = blend_windows([0.0, 4.0, 8.0], blend=6)
        self.assertEqual(w[0], ((0, 1.0),))
        self.assertEqual(frames(w[2]), [5, 6, 7, 8])
        wt = dict(w[2])
        self.assertGreater(wt[8], 2 * wt[5])

    def test_slow_motion_cross_fades(self):
        w = blend_windows([10.0, 10.5, 11.0])
        self.assertIn(11, frames(w[1]))
        self.assertIn(10, frames(w[1]))
        self.assertNotEqual(w[1], w[2])

    def test_holds_flashbacks_and_cuts_stay_sharp(self):
        w = blend_windows([50.0, 50.0, 5.0, 200.0], blend=6)
        self.assertEqual(w[1:], [((50, 1.0),), ((5, 1.0),), ((200, 1.0),)])

    def test_mix_weights(self):
        store = [np.zeros((4, 4, 3), np.uint8), np.full((4, 4, 3), 90, np.uint8)]
        self.assertEqual(int(mix(store, ((0, 1 / 3), (1, 2 / 3)))[0, 0, 0]), 60)

    def test_combined_camera_matches_two_steps(self):
        img = (np.random.default_rng(1).random((320, 180, 3)) * 255).astype(np.uint8)
        self.assertTrue(np.array_equal(fx.camera(img, 0.5, 0.5, 1.0, (0.3, 0.6, 1.4)),
                                       fx.camera(img, 0.3, 0.6, 1.4)))
        self.assertIs(fx.camera(img, 0.5, 0.5, 1.0, (0.5, 0.5, 1.0)), img)


if __name__ == "__main__":
    unittest.main()


class ProcessCutsTest(unittest.TestCase):
    """Process cuts: short real-speed moments from the original, spread through the inking."""

    def test_moments_spread_through_ink_and_skip_arm_frames(self):
        from reels_ai.variants import pick_moments
        n, win = 900, 15
        usable = [not (300 <= i < 330) for i in range(n)]
        prog = list(np.clip((np.arange(n // win) * win - 150) / 600, 0, 1))
        a = SimpleNamespace(n_frames=n, window_frames=win, ink_progress=prog, usable=usable,
                            ink_start=150, ink_end=750)
        m = pick_moments(a, 8)
        self.assertGreaterEqual(len(m), 6)
        self.assertEqual(m, sorted(m))
        self.assertTrue(all(140 <= f <= 765 for f in m), m)
        self.assertFalse(any(300 <= f < 330 for f in m))

    def test_renders_from_original_recording(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        import cv2

        from reels_ai.pipeline import make_variants
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "naruto.mp4"
            w, h, n = 360, 640, 600
            vw = cv2.VideoWriter(str(tmp / "raw.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
            page = np.full((h, w, 3), 235, np.uint8)
            for i in range(n):
                if 60 < i < 560:                                    # ink goes down line by line
                    y = 80 + (i - 60)
                    cv2.line(page, (60, y), (300, y), (20, 20, 20), 1)
                img = page.copy()
                if i < 570:                                         # a hand following the pen
                    cv2.circle(img, (320, 80 + max(0, i - 60) % 480), 40, (120, 160, 210), -1)
                vw.write(img)
            vw.release()
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "raw.avi"), "-pix_fmt",
                            "yuv420p", str(src)], check=True)
            out, res = make_variants(src, tmp / "out", ["process_cuts"], subject="Naruto",
                                     review_page=False)
            self.assertEqual([r["name"] for r in res], ["process_cuts"])
            self.assertTrue(8 <= res[0]["seconds"] <= 15, res[0]["seconds"])
            self.assertTrue((out / "variants" / "process_cuts.mp4").stat().st_size > 10_000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
