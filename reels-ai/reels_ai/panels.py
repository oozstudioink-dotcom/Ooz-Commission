"""Black side panels for vertical recordings in a 16:9 LongDraw.

Left : OOZ / red rule / STUDIO INK (same look as the owner's live-stream layout).
Right: cards rotating every `rotate_minutes`: the owner's commissions advert animation
       (from Drive > Side Panels, placed pixel-for-pixel, never enlarged unless allowed),
       socials, and the upload schedule. Every card (and the left panel at the start)
       glitches in.

Panels are rendered as their own video tracks (mostly static, so they encode fast) and
stacked beside the drawing by longform.py.
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .fx import font
from .media import FPS
from .thumbnail import CREAM, RED, _anton

GREY = (150, 146, 138)
GLITCH_S = 1.0


def _spaced(t: str) -> str:
    return " ".join(t)


def _text(d: ImageDraw.ImageDraw, xy, text, f, fill, anchor="mm"):
    d.text(xy, text, font=f, fill=fill, anchor=anchor)


def left_card(w: int, h: int) -> np.ndarray:
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = w // 2
    _text(d, (cx, int(h * 0.45)), "OOZ", font("black", int(w * 0.30)), CREAM)
    bar_w, bar_h = int(w * 0.11), max(6, int(h * 0.005))
    y = int(h * 0.555)
    d.rectangle((cx - bar_w // 2, y, cx + bar_w // 2, y + bar_h), fill=RED)
    _text(d, (cx, int(h * 0.61)), _spaced("STUDIO INK"), font("bold", int(w * 0.07)), CREAM)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def socials_card(w: int, h: int, socials: list[list[str]], link: str) -> np.ndarray:
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = w // 2
    _text(d, (cx, int(h * 0.22)), _spaced("FOLLOW THE PROCESS"), font("bold", int(w * 0.045)), CREAM)
    d.rectangle((cx - int(w * 0.05), int(h * 0.25), cx + int(w * 0.05), int(h * 0.25) + 6), fill=RED)
    rows = socials[:7]
    top, step = h * 0.32, h * 0.075
    for i, (platform, handle) in enumerate(rows):
        y = int(top + i * step)
        _text(d, (cx, y), _spaced(platform.upper()), font("bold", int(w * 0.03)), GREY)
        _text(d, (cx, y + int(h * 0.03)), handle, font("black", int(w * 0.06)), CREAM)
    if link:
        _text(d, (cx, int(top + len(rows) * step + h * 0.03)), link.upper(), font("bold", int(w * 0.045)), RED)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def schedule_card(w: int, h: int, title: str, lines: list[str]) -> np.ndarray:
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = w // 2
    _text(d, (cx, int(h * 0.30)), _spaced(title.upper()), font("bold", int(w * 0.05)), CREAM)
    d.rectangle((cx - int(w * 0.05), int(h * 0.335), cx + int(w * 0.05), int(h * 0.335) + 6), fill=RED)
    for i, line in enumerate(lines[:6]):
        main, _, sub = line.partition("|")
        y = int(h * 0.42 + i * h * 0.1)
        _text(d, (cx, y), main.strip().upper(), _anton(int(w * 0.11)), CREAM if i % 2 == 0 else RED)
        if sub.strip():
            _text(d, (cx, y + int(h * 0.05)), _spaced(sub.strip().upper()), font("bold", int(w * 0.032)), GREY)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def glitch_frames(card: np.ndarray, n: int = int(GLITCH_S * FPS), seed: int = 7) -> list[np.ndarray]:
    """Black -> card with slice tearing, RGB split and flicker that settle over `n` frames."""
    rng = random.Random(seed)
    h, w = card.shape[:2]
    out = []
    for k in range(n):
        p = (k + 1) / n                          # 0 -> 1
        if p < 0.35 and rng.random() < 0.45:     # early flicker: mostly dark
            out.append(np.zeros_like(card))
            continue
        frame = card.copy()
        amp = int((1 - p) ** 2 * w * 0.12)
        if amp:
            for _ in range(rng.randint(6, 14)):  # torn horizontal slices
                y0 = rng.randint(0, h - 2)
                y1 = min(h, y0 + rng.randint(4, max(5, h // 18)))
                frame[y0:y1] = np.roll(frame[y0:y1], rng.randint(-amp, amp), axis=1)
            s = max(1, amp // 3)                 # red / cyan split
            b, g, r = cv2.split(frame)
            frame = cv2.merge([np.roll(b, -s, axis=1), np.roll(g, -s, axis=1), np.roll(r, s, axis=1)])
        frame = (frame.astype(np.float32) * (0.55 + 0.45 * p)).astype(np.uint8)
        out.append(frame)
    out[-1] = card.copy()
    return out


def advert_clip(src: Path, w: int, h: int, allow_upscale: bool = False) -> tuple[list[np.ndarray], float]:
    """Frames of the owner's advert, cropped to its animated content and centred on a w x h
    black panel. Never enlarged unless allow_upscale (to keep it sharp)."""
    cap = cv2.VideoCapture(str(src))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    if not frames:
        raise RuntimeError(f"couldn't read the advert video {src.name}")
    acc = np.zeros(frames[0].shape[:2], bool)
    for f in frames[:: max(1, len(frames) // 60)]:
        acc |= f.max(axis=2) > 40
    ys, xs = np.nonzero(acc)
    if len(xs):
        m = 24
        x0, x1 = max(0, xs.min() - m), min(frames[0].shape[1], xs.max() + m)
        y0, y1 = max(0, ys.min() - m), min(frames[0].shape[0], ys.max() + m)
    else:
        y0, x0, (y1, x1) = 0, 0, frames[0].shape[:2]
    cw, ch = x1 - x0, y1 - y0
    scale = min((w * 0.9) / cw, (h * 0.9) / ch)
    if not allow_upscale:
        scale = min(scale, 1.0)
    nw, nh = int(cw * scale), int(ch * scale)
    out = []
    for f in frames:                                   # keep only the cropped content in memory
        c = f[y0:y1, x0:x1]
        if scale != 1.0:
            c = cv2.resize(c, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4)
        out.append(np.ascontiguousarray(c))
    return out, scale


def _place(panel: np.ndarray, content: np.ndarray) -> np.ndarray:
    h, w = panel.shape[:2]
    ch, cw = content.shape[:2]
    py, px = (h - ch) // 2, (w - cw) // 2
    panel[py:py + ch, px:px + cw] = content
    return panel


class _Writer:
    """Raw BGR frames -> H.264 panel track (fast preset; panels are mostly static)."""

    def __init__(self, path: Path, w: int, h: int):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)

    def write(self, f: np.ndarray, times: int = 1) -> None:
        b = np.ascontiguousarray(f).tobytes()
        for _ in range(times):
            self.proc.stdin.write(b)

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait():
            raise RuntimeError("panel encode failed")


def render_left(path: Path, w: int, h: int, seconds: float, intro: bool = True) -> Path:
    card = left_card(w, h)
    total = int(round(seconds * FPS))
    wr = _Writer(path, w, h)
    frames = glitch_frames(card, seed=3) if intro else []
    lead = int(0.5 * FPS) if intro else 0               # empty black panels first
    wr.write(np.zeros_like(card), min(lead, total))
    for f in frames[: max(0, total - lead)]:
        wr.write(f)
    wr.write(card, max(0, total - lead - len(frames)))
    wr.close()
    return path


def render_right(path: Path, w: int, h: int, seconds: float, cards: list, start_s: float = 0.0,
                 rotate_s: float = 300.0) -> Path:
    """cards: list of ("still", image) or ("clip", [frames]); slot index follows the global clock
    (start_s) so rotation continues across sessions."""
    total = int(round(seconds * FPS))
    wr = _Writer(path, w, h)
    k = 0
    lead = int(0.5 * FPS) if start_s == 0 else 0
    wr.write(np.zeros((h, w, 3), np.uint8), min(lead, total))
    k += min(lead, total)
    first = True
    black = np.zeros((h, w, 3), np.uint8)
    while k < total:
        t = start_s + k / FPS
        slot = int(t // rotate_s)
        kind, payload = cards[slot % len(cards)]
        slot_end = int(round(((slot + 1) * rotate_s - start_s) * FPS))
        n = min(total, slot_end) - k
        fresh = not (first and start_s % rotate_s > 0.5)     # don't re-glitch a card mid-way
        first = False
        if kind == "still":
            g = glitch_frames(payload, seed=slot) if fresh else []
            for f in g[:n]:
                wr.write(f)
            wr.write(payload, max(0, n - len(g)))
        elif kind == "video":                        # loop a panel-sized clip
            cap = cv2.VideoCapture(str(payload))
            for _ in range(n):
                ok, f = cap.read()
                if not ok:
                    cap.release()
                    cap = cv2.VideoCapture(str(payload))
                    ok, f = cap.read()
                wr.write(f if ok else black)
            cap.release()
        else:
            panel = black.copy()
            for i in range(n):
                wr.write(_place(panel, payload[i % len(payload)]))
        k += n
    wr.close()
    return path


def _clip_to_video(frames: list[np.ndarray], w: int, h: int, out: Path) -> Path:
    wr = _Writer(out, w, h)
    panel = np.zeros((h, w, 3), np.uint8)
    for c in frames:
        wr.write(_place(panel.copy(), c))
    wr.close()
    return out


def build_cards(w: int, h: int, cfg: dict, advert: Path | None, work: Path | None = None) -> list:
    """Right-panel cards. The advert is the built-in recreation (sharp at any size, text from
    config) unless [panels.advert] source = "drive" and a Drive advert video was found."""
    import tempfile
    work = Path(work or tempfile.mkdtemp())
    cards = []
    acfg = cfg.get("advert", {})
    if acfg.get("source", "generated") == "drive" and advert:
        frames, _ = advert_clip(advert, w, h, cfg.get("allow_upscale", False))
        cards.append(("video", _clip_to_video(frames, w, h, work / "advert_panel.mp4")))
    elif acfg.get("source", "generated") != "off":
        from .advert import Advert
        ad = Advert(w, h, w * 0.84, brand=tuple(acfg.get("brand", ("OOZ", "STUDIO", "INK"))),
                    headline=tuple(acfg.get("headline", ("COMMISSIONS", "OPEN"))),
                    offer=tuple(acfg.get("offer", ("FIRST 10 CLIENTS", "50% OFF"))))
        cards.append(("video", ad.render(work / "advert_panel.mp4", crf=12)))
    cards.append(("still", socials_card(w, h, cfg.get("socials", []), cfg.get("link", ""))))
    sched = cfg.get("schedule", [])
    if sched:
        cards.append(("still", schedule_card(w, h, cfg.get("schedule_title", "Upload schedule"), sched)))
    return cards
