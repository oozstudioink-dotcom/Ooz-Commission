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

    def test_mix_blends_page_newest_counts_most(self):
        store = [np.zeros((40, 40, 3), np.uint8), np.full((40, 40, 3), 90, np.uint8)]
        v = int(mix(store, ((0, 1 / 3), (1, 2 / 3)))[0, 0, 0])
        self.assertTrue(50 <= v <= 75, v)

    def test_mix_leaves_no_ghost_arm(self):
        import cv2
        paper = np.full((320, 180, 3), 230, np.uint8)
        old = paper.copy()
        cv2.ellipse(old, (60, 160), (40, 70), 0, 0, 360, (95, 130, 185), -1)   # arm, older frame
        new = paper.copy()
        cv2.ellipse(new, (130, 160), (40, 70), 0, 0, 360, (95, 130, 185), -1)  # arm moved
        out = mix([old, old, new], ((0, 0.2), (1, 0.3), (2, 0.5)))
        self.assertLess(np.abs(out[160, 60].astype(int) - 230).max(), 4)       # no see-through arm
        self.assertLess(np.abs(out[160, 130].astype(int) - new[160, 130]).max(), 4)   # solid hand

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


class AnalysisFixesTest(unittest.TestCase):
    """Bold fills first, thin linework last, no pencil: the edit must cover the thin lines."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        from pathlib import Path

        import cv2

        from reels_ai.analyze import analyze
        from reels_ai.media import FrameStore
        cls.tmp = Path(tempfile.mkdtemp())
        w, h, n = 720, 1280, 900
        vw = cv2.VideoWriter(str(cls.tmp / "s.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
        page = np.full((h, w, 3), 232, np.uint8)
        rng = np.random.default_rng(0)
        for i in range(n):
            p, hand = i / n, None
            if 0.05 < p < 0.45:                      # bold black shapes
                k = int((p - 0.05) / 0.4 * 40)
                x, y = 80 + (k % 8) * 70, 120 + (k // 8) * 70
                cv2.rectangle(page, (x, y), (x + 50, y + 50), (15, 15, 15), -1)
                hand = (x + 60, y + 60)
            elif 0.5 < p < 0.95:                     # thin fine linework afterwards
                k = int((p - 0.5) / 0.45 * 120)
                y, x0 = 700 + (k % 60) * 7, 150 + (k // 60) * 220
                cv2.line(page, (x0, y), (x0 + 180, y + int(rng.integers(-6, 7))), (70, 70, 70), 1, cv2.LINE_AA)
                hand = (x0 + 190, y + 10)
            img = page.copy()
            if hand:
                cv2.ellipse(img, (hand[0] + 60, hand[1] + 90), (70, 110), 30, 0, 360, (95, 130, 185), -1)
            vw.write(img)
        vw.release()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cls.tmp / "s.avi"), "-pix_fmt", "yuv420p",
                        str(cls.tmp / "s.mp4")], check=True)
        cls.a = analyze(FrameStore(cls.tmp / "s.mp4"))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ink_end_waits_for_thin_lines(self):
        self.assertGreater(self.a.ink_end, 0.85 * 900)      # thin lines finish at ~95%

    def test_straight_to_ink_detected(self):
        self.assertFalse(self.a.has_pencil)

    def test_no_pencil_wording(self):
        import random

        from reels_ai import hooks
        from reels_ai.variants import build
        for seed in range(20):
            for kind in hooks.HOOKS:
                h = hooks.pick_hook(kind, random.Random(seed), "Naruto", 9, pencil=False)
                self.assertNotRegex(h.lower(), "(?<!no )sketch|pencil")   # "no sketch" is fine
        plans = build(self.a, ["speedrun", "ink_only", "reveal_first"], 1, "Naruto",
                      copy={"hooks": {"speedrun": "Sketch to ink fast"}, "caption": "Pencil first!"})
        for p in plans:
            self.assertNotRegex((p.hook + p.caption).lower(), "pencil|(?<!no )sketch")
            self.assertTrue(8 <= p.seconds <= 15, (p.name, p.seconds))


class CaptionFitTest(unittest.TestCase):
    def test_long_text_stays_inside_safe_zone(self):
        for w, h in ((1080, 1920), (2160, 3840)):
            for text, style in (("Inking Shinobi Undercover Manga, no talking", "outline"),
                                ("Supercalifragilisticexpialidocious inking", "ink"),
                                ("My Hero Academia in 9 seconds", "tag"),
                                ("Comment a character for next time", "box")):
                y0, y1, x0, x1 = fx._alpha_box(fx.text_layer(text, style, w, h))
                self.assertGreaterEqual(x0, w * 0.09, text)
                self.assertLessEqual(x1, w * 0.91, text)
                self.assertGreaterEqual(y0, h * 0.12, text)
                self.assertLessEqual(y1, h * 0.30, text)


class HonestCopyTest(unittest.TestCase):
    def test_claims_checked_against_the_session(self):
        from reels_ai.hooks import honest
        self.assertFalse(honest("Real time inking session", True))
        self.assertFalse(honest("Pure ink, no pencil sketches", True))
        self.assertTrue(honest("Pure ink, no pencil sketches", False))
        self.assertFalse(honest("From pencil sketch to clean ink", False))
        self.assertTrue(honest("From pencil sketch to clean ink", True))
        self.assertTrue(honest("Watch every line go down", True))


class ContinuedPageTest(unittest.TestCase):
    """A session that adds a little ink to a page that's already mostly inked."""

    def test_old_ink_is_not_this_sessions_start(self):
        import subprocess
        import tempfile
        from pathlib import Path

        import cv2

        from reels_ai.analyze import analyze
        from reels_ai.media import FrameStore
        tmp = Path(tempfile.mkdtemp())
        w, h, n = 720, 1280, 600
        page = np.full((h, w, 3), 232, np.uint8)
        cv2.rectangle(page, (60, 80), (660, 700), (15, 15, 15), -1)          # last session's ink
        vw = cv2.VideoWriter(str(tmp / "c.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
        for i in range(n):
            if 200 < i < 560:                                                # today: small figure
                k = (i - 200) // 4
                y, x = 800 + (k % 30) * 12, 150 + (k // 30) * 40
                cv2.line(page, (x, y), (x + 30, y + 8), (20, 20, 20), 2)
            vw.write(page)
        vw.release()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "c.avi"), "-pix_fmt", "yuv420p",
                        str(tmp / "c.mp4")], check=True)
        a = analyze(FrameStore(tmp / "c.mp4"))
        self.assertGreater(a.ink_start, 150, a.ink_start)
        self.assertGreater(a.peak_idx, 150, a.peak_idx)
        self.assertGreater(a.ink_end, 450, a.ink_end)


class NamingTest(unittest.TestCase):
    def test_tags_in_names(self):
        from reels_ai.drive import describe_hints, parse_name
        self.assertEqual(parse_name("Sasuke - ink only - continued.MOV"),
                         ("Sasuke", {"pencil": False, "continued": True}))
        self.assertEqual(parse_name("Goku vs Frieza - Pencil.mov"), ("Goku vs Frieza", {"pencil": True}))
        self.assertEqual(parse_name("Naruto [no pencil].MOV"), ("Naruto", {"pencil": False}))
        self.assertEqual(parse_name("IMG_0157.MOV"), (None, {}))
        self.assertEqual(parse_name("3.MOV"), (None, {}))
        self.assertEqual(parse_name("Luffy - blah.MOV")[1], {"unknown": ["blah"]})
        self.assertEqual(describe_hints({"pencil": False, "continued": True}), "ink only · continued page")

    def test_longdraw_parts_in_number_order(self):
        from reels_ai.drive import session_order
        from reels_ai.longform import subject_title
        files = [{"name": n, "createdTime": t} for n, t in
                 [("10.MOV", "1"), ("2.MOV", "5"), ("1.MOV", "9"), ("Part 3.mov", "0"), ("extra.mov", "0")]]
        self.assertEqual([f["name"] for f in sorted(files, key=session_order)],
                         ["1.MOV", "2.MOV", "Part 3.mov", "10.MOV", "extra.mov"])
        self.assertEqual(subject_title("2.MOV", 1), "Part 2")
        self.assertEqual(subject_title("IMG_0157.MOV", 4), "Session 5")

    def test_hints_override_detection(self):
        import subprocess
        import tempfile
        from pathlib import Path

        import cv2

        from reels_ai.analyze import analyze
        from reels_ai.media import FrameStore
        tmp = Path(tempfile.mkdtemp())
        page = np.full((640, 360, 3), 232, np.uint8)
        vw = cv2.VideoWriter(str(tmp / "p.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (360, 640))
        for i in range(240):
            if 30 < i < 220:
                k = i - 30
                cv2.line(page, (40 + (k % 20) * 14, 100 + (k // 20) * 30),
                         (50 + (k % 20) * 14, 120 + (k // 20) * 30), (20, 20, 20), 2)
            vw.write(page)
        vw.release()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "p.avi"), "-pix_fmt", "yuv420p",
                        str(tmp / "p.mp4")], check=True)
        store = FrameStore(tmp / "p.mp4")
        self.assertTrue(analyze(store, pencil=True).has_pencil)
        self.assertFalse(analyze(store, pencil=False).has_pencil)
