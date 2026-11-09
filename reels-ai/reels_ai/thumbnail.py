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


def crop_art(frame_bgr: np.ndarray, aspect: float | None = None) -> Image.Image:
    """Crop a final frame to the finished drawing so it fills the thumbnail's art panel.

    Finds the paper (largest bright, low-colour region), then the ink on it (ignoring a thin
    border, where spiral binding and page edges sit), and frames the ink with a little margin at
    the art panel's shape. A bit of paper or table at the edges is fine; the drawing leads."""
    from .fx import grade
    if aspect is None:
        x0, y0, x1, y1 = BOXES["art"]
        aspect = (x1 - x0) / (y1 - y0)
    frame_bgr = grade(frame_bgr, "clean")                # white paper, true blacks
    H, W = frame_bgr.shape[:2]
    sw = 480
    small = cv2.resize(frame_bgr, (sw, int(sw * H / W)))
    s = W / sw
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    paper = ((hsv[..., 2] > 150) & (hsv[..., 1] < 60)).astype(np.uint8)
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(paper)
    px, py, pw, ph = 0, 0, small.shape[1], small.shape[0]
    sheet = np.ones(paper.shape, bool)
    if n > 1:
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[i, cv2.CC_STAT_AREA] > 0.15 * small.shape[0] * small.shape[1]:
            px, py, pw, ph = stats[i, :4]
            # the sheet itself with the ink on it (holes filled), minus a thin border where spiral
            # binding and page edges sit: table, hands and on-screen text are outside it
            m = (labels == i).astype(np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((41, 41), np.uint8))
            cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            filled = np.zeros_like(m)
            cv2.drawContours(filled, cnts, -1, 1, -1)
            sheet = cv2.erode(filled, np.ones((9, 9), np.uint8)) > 0
    # ink on the sheet
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = cv2.GaussianBlur(cv2.dilate(gray, np.ones((15, 15), np.uint8)), (0, 0), 5)
    ink = (local - gray) > 45
    bx, by = int(pw * 0.04), int(ph * 0.02)
    region = np.zeros_like(ink)
    region[py + by:py + ph - by, px + bx:px + pw - bx] = True
    ys, xs = np.nonzero(ink & region & sheet)
    if len(xs) < 50:                                     # no clear ink: show the paper
        x0, y0, x1, y1 = px, py, px + pw, py + ph
    else:
        x0, x1 = np.percentile(xs, [1, 99])
        y0, y1 = np.percentile(ys, [1, 99])
        mx, my = (x1 - x0) * 0.08 + 4, (y1 - y0) * 0.08 + 4
        x0, x1, y0, y1 = x0 - mx, x1 + mx, y0 - my, y1 + my
    # grow the short side to the panel's shape; where that runs off the frame (a tall drawing in
    # a vertical video), pad with the thumbnail background rather than cut the drawing
    cw, ch = x1 - x0, y1 - y0
    if cw / ch < aspect:
        cw = ch * aspect
    else:
        ch = cw / aspect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    fw, fh = small.shape[1], small.shape[0]
    x0 = cx - cw / 2 if cw > fw else max(0, min(cx - cw / 2, fw - cw))
    y0 = cy - ch / 2 if ch > fh else max(0, min(cy - ch / 2, fh - ch))
    X0, Y0, CW, CH = int(x0 * s), int(y0 * s), int(cw * s), int(ch * s)
    pad = max(0, -X0, -Y0, X0 + CW - W, Y0 + CH - H)
    if pad:
        frame_bgr = cv2.copyMakeBorder(frame_bgr, pad, pad, pad, pad, cv2.BORDER_CONSTANT,
                                       value=tuple(int(c) for c in BG[::-1]))
        X0, Y0 = X0 + pad, Y0 + pad
    crop = frame_bgr[Y0:Y0 + CH, X0:X0 + CW]
    return Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))


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


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(w, int(img.width * scale)), max(h, int(img.height * scale))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def make_thumbnail(art: Image.Image, episode: int, out: Path, size: tuple[int, int] = (1920, 1080),
                   logo: Image.Image | None = None, words: tuple[str, str] = ("LONG", "DRAW")) -> Path:
    w, h = size
    canvas = Image.new("RGB", size, BG)

    # finished art panel with a soft shadow
    ax0, ay0, ax1, ay1 = _box("art", w, h)
    shadow = Image.new("L", size, 0)
    sd = ImageDraw.Draw(shadow)
    off = int(w * 0.012)
    sd.rectangle((ax0 + off, ay0 + off, ax1 + off, ay1 + off), fill=200)
    shadow = shadow.filter(ImageFilter.GaussianBlur(w * 0.012))
    canvas.paste(Image.new("RGB", size, (0, 0, 0)), (0, 0), shadow)
    canvas.paste(_fit_cover(art.convert("RGB"), ax1 - ax0, ay1 - ay0), (ax0, ay0))

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
