"""The commissions advert, rebuilt in code from the owner's original 9 s animation.

Rendered fresh at any size (so it is always pixel-sharp), centred, with editable text.
Timeline (9 s seamless loop), measured from the original:
  0.0-2.9  crosshair-circle logo + OOZ / STUDIO / INK; five red dots drip down the centre
           line (0.5-1.7 s) then drain top-first (1.9-2.8 s)
  2.9-3.2  logo fades out
  3.8-5.4  COMMISSIONS / OPEN (cream) fades in rising slightly, then out
  5.6-6.9  FIRST 10 CLIENTS / 50% OFF (red, right-aligned) fades in, then out
  7.6-9.0  logo returns (text first, circle just after) -> loops to frame 0
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .media import FPS
from .thumbnail import _anton

CREAM = (230, 223, 200)          # RGB, sampled from the original (#E0D9C0, lifted slightly)
RED = (186, 58, 52)              # RGB, sampled from the original (#AF3934)
SECONDS = 9.0

# Layout in the original's units (its block is ~520 x 250); everything scales from here.
REF_W, REF_H = 520.0, 250.0
CIRCLE = (92.0, 123.0, 92.0)     # cx, cy, r  (relative to the block's top-left)
DOTS_Y = [60, 90, 120, 150, 180]
DOT_R = 7.5


def _ramp(t: float, a: float, b: float) -> float:
    return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)


def _env(t: float, fin: tuple[float, float], fout: tuple[float, float]) -> float:
    return min(_ramp(t, *fin), 1.0 - _ramp(t, *fout))


def _text_layer(size: tuple[int, int], lines: list[str], x: float, top: float, cap: list[float],
                gap: float, color, align: str = "left") -> np.ndarray:
    """RGB layer with stacked condensed lines; cap = target cap height per line (px)."""
    img = Image.new("RGB", size, (0, 0, 0))
    d = ImageDraw.Draw(img)
    y = top
    for line, ch in zip(lines, cap):
        f = _anton(max(8, int(ch * 1.38)))
        l, t, r, b = d.textbbox((0, 0), line, font=f)
        px = x - l if align == "left" else x - r
        d.text((px, y - t), line, font=f, fill=color)
        y += (b - t) + gap
    return np.asarray(img).astype(np.float32)


class Advert:
    def __init__(self, width: int, height: int, content_width: float, brand=("OOZ", "STUDIO", "INK"),
                 headline=("COMMISSIONS", "OPEN"), offer=("FIRST 10 CLIENTS", "50% OFF")):
        self.w, self.h = width, height
        s = self.s = content_width / REF_W
        self.ox = (width - REF_W * s) / 2               # block's top-left on the canvas
        self.oy = (height - REF_H * s) / 2
        X = lambda v: self.ox + v * s                   # noqa: E731
        Y = lambda v: self.oy + v * s                   # noqa: E731
        self.X, self.Y = X, Y
        size = (width, height)
        self.brand = _text_layer(size, list(brand), X(243), Y(0), [70 * s] * 3, 10 * s, CREAM)
        self.headline = _text_layer(size, list(headline), X(76), Y(93), [64 * s, 64 * s], 12 * s, CREAM)
        self.offer = _text_layer(size, list(offer), X(518), Y(98), [56 * s, 70 * s], 12 * s, RED, "right")
        self.circle = self._circle_layer()
        ys, xs = np.nonzero(np.maximum.reduce([self.brand.max(2), self.headline.max(2), self.offer.max(2),
                                               self.circle.max(2)]) > 0)
        pad = int(40 * s)                               # only this region ever changes
        self.box = (max(0, xs.min() - pad), max(0, ys.min() - pad - int(20 * s)),
                    min(width, xs.max() + pad), min(height, ys.max() + pad))

    def _circle_layer(self) -> np.ndarray:
        s, X, Y = self.s, self.X, self.Y
        cx, cy, r = X(CIRCLE[0]), Y(CIRCLE[1]), CIRCLE[2] * s
        k = 8                                           # draw supersampled with AA, then shrink
        big = np.zeros((self.h * 1, self.w * 1, 3), np.uint8)
        layer = np.zeros_like(big)
        col = CREAM[::-1]
        th = max(1, int(round(2.2 * s)))
        sh = 4
        c = (int(cx * 2 ** sh), int(cy * 2 ** sh))
        cv2.circle(layer, c, int(r * 2 ** sh), col, th, cv2.LINE_AA, sh)
        tick = 16 * s
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):   # crosshair ticks crossing the ring
            p1 = (int((cx + dx * (r - tick)) * 2 ** sh), int((cy + dy * (r - tick)) * 2 ** sh))
            p2 = (int((cx + dx * (r + tick)) * 2 ** sh), int((cy + dy * (r + tick)) * 2 ** sh))
            cv2.line(layer, p1, p2, col, th, cv2.LINE_AA, sh)
        dim = tuple(int(v * 0.35) for v in col)           # faint dotted centre lines
        for i in range(-int(r * 0.85 / (9 * s)), int(r * 0.85 / (9 * s)) + 1):
            for p in ((cx + i * 9 * s, cy), (cx, cy + i * 9 * s)):
                cv2.circle(layer, (int(p[0] * 2 ** sh), int(p[1] * 2 ** sh)), int(1.1 * s * 2 ** sh),
                           dim, -1, cv2.LINE_AA, sh)
        del big, k
        return layer[..., ::-1].astype(np.float32)        # to RGB like the text layers

    def frame(self, i: int) -> np.ndarray:
        """BGR frame i of the loop."""
        t = (i % int(SECONDS * FPS)) / FPS
        a_brand = max(_env(t, (-1, 0), (2.9, 3.2)), _ramp(t, 7.6, 8.0))
        a_circle = max(_env(t, (-1, 0), (2.9, 3.2)), _ramp(t, 7.9, 8.3))
        a_head = _env(t, (3.8, 4.2), (5.2, 5.4))
        a_offer = _env(t, (5.6, 5.9), (6.7, 6.9))
        x0, y0, x1, y1 = self.box
        sl = (slice(y0, y1), slice(x0, x1))
        out = np.maximum(self.brand[sl] * a_brand, self.circle[sl] * a_circle)
        if a_head > 0:
            rise = int((1 - _ramp(t, 3.8, 4.2)) * 14 * self.s)   # slides up into place
            head = np.roll(self.headline, rise, axis=0)[sl]
            out = np.maximum(out, head * a_head)
        if a_offer > 0:
            out = np.maximum(out, self.offer[sl] * a_offer)
        region = np.ascontiguousarray(out[..., ::-1]).astype(np.uint8)
        # red dots: drip in one by one, then drain top-first
        cx, cy0 = self.X(CIRCLE[0]) - x0, self.Y(0) - y0
        for k, dy in enumerate(DOTS_Y):
            on = _ramp(t, 0.5 + 0.3 * k, 0.6 + 0.3 * k) * (1 - _ramp(t, 1.9 + 0.2 * k, 2.05 + 0.2 * k))
            if on > 0:
                col = tuple(int(v * on) for v in RED[::-1])
                rr = DOT_R * self.s * (0.6 + 0.4 * on)
                cv2.circle(region, (int(cx * 16), int((cy0 + dy * self.s) * 16)), int(rr * 16), col, -1,
                           cv2.LINE_AA, 4)
        frame = np.zeros((self.h, self.w, 3), np.uint8)
        frame[sl] = region
        return frame

    def render(self, out: Path, seconds: float = SECONDS, crf: int = 14) -> Path:
        p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                              "-s", f"{self.w}x{self.h}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
                              "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                              "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
        for i in range(int(round(seconds * FPS))):
            p.stdin.write(self.frame(i).tobytes())
        p.stdin.close()
        if p.wait():
            raise RuntimeError("advert encode failed")
        return Path(out)
