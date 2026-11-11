"""The reviewer (review.py): what must fail and what must pass.

Thresholds were set on real videos (not kept in the repo): three Shorts the owner rejected - a
different page at the start and end, a different page inside a before/after wipe, 33% repeated
frames - all fail; two reference clips and 44 good renders of every style pass. These synthetic
cases keep the same behaviour pinned down.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from reels_ai.review import review


def strokes(seed: int, centres) -> list:
    rng = np.random.default_rng(seed)
    out = []
    for cx, cy in centres:
        for _ in range(70):
            x, y = cx + int(rng.integers(-70, 70)), cy + int(rng.integers(-70, 70))
            dx, dy = (int(v) for v in rng.integers(-34, 34, 2))
            out.append((x, y, x + dx, y + dy))
    return out


def page(lines, upto: int, hand: tuple[int, int] | None = None) -> np.ndarray:
    img = np.full((640, 360, 3), 240, np.uint8)
    for x1, y1, x2, y2 in lines[:upto]:
        cv2.line(img, (x1, y1), (x2, y2), (25, 25, 25), 5)
    if hand:
        cv2.ellipse(img, hand, (40, 60), 30, 0, 360, (90, 130, 190), -1)
    return img


def drawing(n: int, seed: int = 1, centres=((120, 260), (240, 400))) -> list[np.ndarray]:
    lines = strokes(seed, centres)
    return [page(lines, 1 + i * len(lines) // n, (60 + (i * 7) % 240, 330 + (i * 5) % 200)) for i in range(n)]


def write(frames: list[np.ndarray], path: Path) -> Path:
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "360x640",
                          "-r", "30", "-i", "-", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(path)],
                         stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    p.wait()
    return path


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_one_page_from_first_line_to_last_passes(self):
        r = review(write(drawing(270), self.tmp / "ok.mp4"))
        self.assertTrue(r.ok, r.reasons)
        self.assertEqual(r.foreign, [])

    def test_reveal_first_passes(self):                      # finished piece, then back to the start
        frames = drawing(240)
        r = review(write([frames[-1]] * 30 + frames, self.tmp / "reveal.mp4"))
        self.assertTrue(r.ok, r.reasons)

    def test_another_page_at_the_end_fails(self):
        other = page(strokes(9, ((110, 480), (250, 250))), 999)          # as dense as the drawing
        r = review(write(drawing(240) + [other] * 60, self.tmp / "flip.mp4"))
        self.assertFalse(r.ok)
        self.assertTrue(any("different page" in why for why in r.reasons), r.reasons)
        self.assertTrue(all(t > 7.5 for t in r.foreign), r.foreign)       # and says where

    def test_stutter_fails(self):
        frames = [f for f in drawing(90) for _ in range(3)]              # every frame shown 3 times
        r = review(write(frames, self.tmp / "stutter.mp4"))
        self.assertFalse(r.ok)
        self.assertTrue(any("stutters" in why for why in r.reasons), r.reasons)

    def test_too_short_fails(self):
        r = review(write(drawing(120), self.tmp / "short.mp4"))          # 4 seconds
        self.assertFalse(r.ok)
        self.assertTrue(any("long" in why for why in r.reasons), r.reasons)


if __name__ == "__main__":
    unittest.main()
