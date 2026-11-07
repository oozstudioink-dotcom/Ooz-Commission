"""Per-frame effects: camera moves, colour grades, text cards, wipes, progress bar."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [Path(__file__).resolve().parent.parent / "fonts",
             Path("/usr/share/fonts/opentype/inter"), Path("/usr/share/fonts/truetype/inter"),
             Path("/usr/share/fonts/truetype/dejavu")]
FONT_PREFS = {
    "black": ["InterDisplay-Black.otf", "Inter-Black.otf", "Inter-Black.ttf", "DejaVuSans-Bold.ttf"],
    "bold": ["InterDisplay-Bold.otf", "Inter-Bold.otf", "Inter-Bold.ttf", "DejaVuSans-Bold.ttf"],
}


@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONT_PREFS[weight]:
        for d in FONT_DIRS:
            p = d / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- easing / camera
def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def camera(img: np.ndarray, cx: float, cy: float, zoom: float) -> np.ndarray:
    """Sub-pixel smooth zoom centred on (cx, cy) in 0..1 coords; clamps to stay on-frame."""
    if zoom <= 1.0001:
        return img
    h, w = img.shape[:2]
    cw, ch = w / zoom, h / zoom
    x0 = min(max(cx * w - cw / 2, 0), w - cw)
    y0 = min(max(cy * h - ch / 2, 0), h - ch)
    m = np.float32([[zoom, 0, -x0 * zoom], [0, zoom, -y0 * zoom]])
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


# ---------------------------------------------------------------- grades
def _lut(fn) -> np.ndarray:
    return np.clip([fn(v) for v in range(256)], 0, 255).astype(np.uint8)


def grade(img: np.ndarray, name: str) -> np.ndarray:
    if name == "natural":
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    paper = float(np.percentile(gray, 85)) or 1.0
    if name == "clean":       # paper pushed to white, keep a touch of colour for skin
        gain = 248.0 / paper
        out = cv2.convertScaleAbs(img, alpha=gain)
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
        hsv[..., 1] = (hsv[..., 1] * 0.55).astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if name == "warm":        # cream paper, sepia-ish ink (desaturate first so room lighting can't tint it)
        gain = 242.0 / paper
        g = cv2.convertScaleAbs(gray, alpha=gain)
        out = cv2.addWeighted(cv2.convertScaleAbs(img, alpha=gain), 0.25,
                              cv2.cvtColor(g, cv2.COLOR_GRAY2BGR), 0.75, 0).astype(np.float32)
        out[..., 0] *= 0.86
        out[..., 1] *= 0.95
        out[..., 2] *= 1.03
        return np.clip(out, 0, 255).astype(np.uint8)
    if name == "mono":        # crisp black & white, punchy ink
        g = cv2.convertScaleAbs(gray, alpha=250.0 / paper)
        lut = _lut(lambda v: 255 * (1 / (1 + np.exp(-(v - 120) / 28))))
        g = cv2.LUT(g, lut)
        return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    raise ValueError(f"unknown grade {name}")


# ---------------------------------------------------------------- text
TEXT_STYLES = ("outline", "box", "ink", "tag")


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, max_w: int, gap: float) -> list[str]:
    """Greedy wrap, then narrow the width as far as possible without adding a line (no orphans)."""
    def measure(line: str) -> float:
        words = line.split()
        return sum(draw.textlength(w, font=f) for w in words) + gap * (len(words) - 1)

    def greedy(width: float) -> list[str]:
        lines: list[str] = []
        for para in text.split("\n"):
            cur = ""
            for word in para.split():
                trial = f"{cur} {word}".strip()
                if measure(trial) <= width or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        return lines

    best = greedy(max_w)
    width = max_w
    while width > max_w * 0.4:
        width -= max_w * 0.03
        trial = greedy(width)
        if len(trial) > len(best):
            break
        best = trial
    return best


@lru_cache(maxsize=64)
def text_layer(text: str, style: str, w: int, h: int, y_frac: float = 0.11) -> np.ndarray:
    """RGBA layer (as BGRA numpy) with the text placed in the top safe zone."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = int(w * (0.078 if style != "tag" else 0.05))
    f = font("black" if style in ("outline", "ink") else "bold", size)
    stroke = max(2, size // 16) if style == "outline" else max(2, size // 18) if style == "ink" else 0
    gap = d.textlength(" ", font=f) + stroke * 2
    lines = _wrap(d, text, f, int(w * 0.82), gap)
    lh = int(size * 1.12)
    y = int(h * y_frac)
    pad = int(size * 0.35)
    for line in lines:
        words = line.split()
        tw = sum(d.textlength(wd, font=f) for wd in words) + gap * (len(words) - 1)
        x = (w - tw) / 2
        if style == "box":
            d.rounded_rectangle((x - pad + 3, y - pad * 0.45 + 5, x + tw + pad + 3, y + lh + pad * 0.15 + 5),
                                radius=pad, fill=(0, 0, 0, 60))            # soft drop shadow
            d.rounded_rectangle((x - pad, y - pad * 0.45, x + tw + pad, y + lh + pad * 0.15),
                                radius=pad, fill=(255, 255, 255, 250), outline=(20, 20, 20, 255),
                                width=max(2, size // 22))
        elif style == "tag":
            d.rounded_rectangle((x - pad, y - pad * 0.4, x + tw + pad, y + lh), radius=pad // 2,
                                fill=(10, 10, 10, 225))
        for wd in words:
            if style == "outline":
                d.text((x, y), wd, font=f, fill=(255, 255, 255, 255),
                       stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            elif style == "ink":
                d.text((x, y), wd, font=f, fill=(12, 12, 12, 255),
                       stroke_width=stroke, stroke_fill=(255, 255, 255, 230))
            elif style == "box":
                d.text((x, y), wd, font=f, fill=(0, 0, 0, 255))
            else:
                d.text((x, y), wd, font=f, fill=(255, 255, 255, 255))
            x += d.textlength(wd, font=f) + gap
        y += lh + (pad if style in ("box", "tag") else 0)
    arr = np.array(layer)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)


def overlay(img: np.ndarray, layer_bgra: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    if alpha <= 0:
        return img
    a = layer_bgra[..., 3:4].astype(np.float32) / 255.0 * alpha
    out = img.astype(np.float32) * (1 - a) + layer_bgra[..., :3].astype(np.float32) * a
    return out.astype(np.uint8)


# ---------------------------------------------------------------- transitions / widgets
def wipe(a: np.ndarray, b: np.ndarray, pos: float) -> np.ndarray:
    """Left of the seam shows `b` (after), right shows `a` (before)."""
    h, w = a.shape[:2]
    x = int(round(w * min(max(pos, 0), 1)))
    out = a.copy()
    out[:, :x] = b[:, :x]
    if 0 < x < w:
        cv2.line(out, (x, 0), (x, h), (255, 255, 255), max(3, w // 160))
        cv2.line(out, (x, 0), (x, h), (30, 30, 30), 1)
    return out


def flash(img: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0:
        return img
    return cv2.addWeighted(img, 1 - amount, np.full_like(img, 255), amount, 0)


def progress_bar(img: np.ndarray, frac: float) -> np.ndarray:
    h, w = img.shape[:2]
    bh = max(6, h // 200)
    y = int(h * 0.035)
    out = img.copy()
    cv2.rectangle(out, (0, y), (w, y + bh), (235, 235, 235), -1)
    cv2.rectangle(out, (0, y), (int(w * min(max(frac, 0), 1)), y + bh), (20, 20, 20), -1)
    return out
