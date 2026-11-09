"""LongDraw YouTube thumbnail in the Ooz Studio Ink house style.

Layout measured from the owner's EP. 01 thumbnail (2000x1125 reference):
  dark #131313 background · signature logo box top-left · outlined "EP. 0N" badge ·
  giant "LONG" (cream) over "DRAW" (red) in Anton · finished-art panel on the right
  with a soft drop shadow.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .fx import font

ROOT = Path(__file__).resolve().parent.parent
ANTON = ROOT / "fonts" / "Anton-Regular.ttf"
LOGO = ROOT / "assets" / "logo_box.png"

BG, CREAM, RED = (19, 19, 19), (242, 239, 229), (224, 73, 58)
REF_W, REF_H = 2000, 1125
BOXES = {                      # reference-space boxes (x0, y0, x1, y1)
    "logo": (100, 88, 537, 231),
    "ep": (575, 125, 776, 193),
    "long": (110, 271, 883, 640),
    "draw": (110, 654, 1010, 1016),
    "art": (1045, 149, 1917, 978),
}


def _box(name: str, w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = BOXES[name]
    sx, sy = w / REF_W, h / REF_H
    return int(x0 * sx), int(y0 * sy), int(x1 * sx), int(y1 * sy)


def _anton(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ANTON), size) if ANTON.exists() else font("black", size)


def _big_word(draw: ImageDraw.ImageDraw, word: str, box, color) -> None:
    """Draw `word` so its ink fills the box height, left/top aligned like the reference."""
    x0, y0, x1, y1 = box
    target_h = y1 - y0
    size = target_h
    for _ in range(4):                                   # converge on the cap height
        f = _anton(size)
        l, t, r, b = draw.textbbox((0, 0), word, font=f)
        size = max(10, int(size * target_h / max(1, b - t)))
    f = _anton(size)
    l, t, r, b = draw.textbbox((0, 0), word, font=f)
    if r - l > (x1 - x0) * 1.08:                         # very long words: keep inside the left half
        f = _anton(int(size * (x1 - x0) * 1.08 / (r - l)))
        l, t, r, b = draw.textbbox((0, 0), word, font=f)
    draw.text((x0 - l, y0 - t), word, font=f, fill=color)


def crop_art(frame_bgr: np.ndarray) -> Image.Image:
    """The whole sheet of paper from a final frame, like a scanned comic page: every drawing on
    the page stays in, the table around it goes (largest bright, low-colour region)."""
    from .fx import grade
    frame_bgr = grade(frame_bgr, "clean")                # white paper, true blacks
    small = cv2.resize(frame_bgr, (480, int(480 * frame_bgr.shape[0] / frame_bgr.shape[1])))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    paper = ((hsv[..., 2] > 150) & (hsv[..., 1] < 60)).astype(np.uint8)
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(paper)
    if n > 1:
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, w, h = stats[i, :4]
        s = frame_bgr.shape[1] / small.shape[1]
        if w * h > 0.15 * small.shape[0] * small.shape[1]:
            # keep only rows/columns that are mostly paper: bright bits of table, skin or sleeves
            # touching the sheet don't stretch the crop
            comp = labels[y:y + h, x:x + w] == i
            rows = np.nonzero(comp.mean(axis=1) > 0.6)[0]
            cols = np.nonzero(comp.mean(axis=0) > 0.6)[0]
            if len(rows) > 10 and len(cols) > 10:
                y, h = y + rows[0], rows[-1] - rows[0] + 1
                x, w = x + cols[0], cols[-1] - cols[0] + 1
            frame_bgr = frame_bgr[int(y * s):int((y + h) * s), int(x * s):int((x + w) * s)]
    return Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))


def clearest(frames: list[np.ndarray]) -> np.ndarray:
    """The frame showing the most finished drawing: most visible ink, i.e. the latest lines with
    no hand, arm or eraser over them."""
    from .analyze import _skin

    def visible_ink(img):
        small = cv2.resize(img, (360, max(1, int(360 * img.shape[0] / img.shape[1]))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        local = cv2.GaussianBlur(cv2.dilate(gray, np.ones((15, 15), np.uint8)), (0, 0), 5)
        ink = int(((local - gray) > 70).sum())          # dark ink only (not pencil)
        return ink * max(0.0, 1 - 2 * float(_skin(small).mean()))
    return max(frames, key=visible_ink)


def _fit_contain(img: Image.Image, w: int, h: int) -> Image.Image:
    """The whole image inside w x h (nothing cut off), centred on the thumbnail background."""
    scale = min(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    out = Image.new("RGB", (w, h), BG)
    out.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return out


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(w, int(img.width * scale)), max(h, int(img.height * scale))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def make_thumbnail(art: Image.Image, episode: int, out: Path, size: tuple[int, int] = (1920, 1080),
                   logo: Image.Image | None = None, words: tuple[str, str] = ("LONG", "DRAW")) -> Path:
    w, h = size
    canvas = Image.new("RGB", size, BG)

    # finished page: the whole sheet (every drawing on it), with a soft shadow that hugs the paper
    # like a scanned page laid on the table
    ax0, ay0, ax1, ay1 = _box("art", w, h)
    art = art.convert("RGB")
    pw, ph = ax1 - ax0, ay1 - ay0
    scale = min(pw / art.width, ph / art.height)
    nw, nh = max(1, int(art.width * scale)), max(1, int(art.height * scale))
    ax0, ay0 = ax0 + (pw - nw) // 2, ay0 + (ph - nh) // 2
    ax1, ay1 = ax0 + nw, ay0 + nh
    shadow = Image.new("L", size, 0)
    sd = ImageDraw.Draw(shadow)
    off = int(w * 0.012)
    sd.rectangle((ax0 + off, ay0 + off, ax1 + off, ay1 + off), fill=200)
    shadow = shadow.filter(ImageFilter.GaussianBlur(w * 0.012))
    canvas.paste(Image.new("RGB", size, (0, 0, 0)), (0, 0), shadow)
    canvas.paste(art.resize((nw, nh), Image.LANCZOS), (ax0, ay0))

    d = ImageDraw.Draw(canvas)
    # signature logo box
    lx0, ly0, lx1, ly1 = _box("logo", w, h)
    logo = logo or (Image.open(LOGO).convert("RGB") if LOGO.exists() else None)
    if logo is not None:
        canvas.paste(_fit_cover(logo, lx1 - lx0, ly1 - ly0), (lx0, ly0))
    else:
        d.rectangle((lx0, ly0, lx1, ly1), fill=CREAM)
        f = font("black", int((ly1 - ly0) * 0.32))
        d.text(((lx0 + lx1) / 2, (ly0 + ly1) / 2), "Ooz Studio Ink", font=f, fill=BG, anchor="mm")

    # episode badge
    ex0, ey0, ex1, ey1 = _box("ep", w, h)
    d.rectangle((ex0, ey0, ex1, ey1), outline=CREAM, width=max(2, int(w * 0.0016)))
    ef = font("black", int((ey1 - ey0) * 0.5))
    text = f"EP. {episode:02d}"
    spaced = " ".join(text.replace(" ", " "))           # tracked-out look
    d.text(((ex0 + ex1) / 2, (ey0 + ey1) / 2), spaced, font=ef, fill=CREAM, anchor="mm")

    # LONG / DRAW
    _big_word(d, words[0], _box("long", w, h), CREAM)
    _big_word(d, words[1], _box("draw", w, h), RED)

    out = Path(out)
    canvas.save(out, quality=92) if out.suffix.lower() in (".jpg", ".jpeg") else canvas.save(out)
    return out


def youtube_jpeg(src: Path, dst: Path, max_bytes: int = 2_000_000) -> Path:
    """1280x720 JPEG under YouTube's 2 MB thumbnail limit."""
    dst = Path(dst)
    img = Image.open(src).convert("RGB").resize((1280, 720), Image.LANCZOS)
    for q in (92, 85, 78, 70):
        img.save(dst, quality=q, optimize=True)
        if dst.stat().st_size <= max_bytes:
            break
    return dst
