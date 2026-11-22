"""YouTube channel art: banner (2560x1440) and profile picture (800x800).

Run from the repo root:  python youtube/make_youtube.py
Everything that matters sits inside YouTube's 1546x423 safe area, the part every device shows.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "youtube"
BG, PAPER, RED, GREY = (21, 21, 21), (245, 244, 240), (192, 57, 43), (170, 170, 165)
FONTS = Path("/usr/share/fonts/opentype/inter")
W, H = 2560, 1440
SAFE = (507, 508, 2053, 931)                        # x0, y0, x1, y1


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def mark(size):
    """The studio mark: a ring, a line and four red dots."""
    im = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d, s, c = ImageDraw.Draw(im), size * 4, size * 2
    ring, r = s * 0.028, s * 0.5 - s * 0.028
    d.ellipse([c - r, c - r, c + r, c + r], outline=PAPER, width=int(ring))
    d.rectangle([s * 0.15, c - s * 0.011, s * 0.85, c + s * 0.011], fill=PAPER)
    for dy in (-0.32, -0.16, 0.16, 0.32):
        y, dot = c + dy * s, s * 0.05
        d.ellipse([c - dot, y - dot, c + dot, y + dot], fill=RED)
    return im.resize((size, size), Image.LANCZOS)


def banner():
    im = Image.new("RGB", (W, H), BG)
    art = Image.open(ROOT / "portfolio" / "01-samurai.jpg").convert("L")
    art = art.resize((int(art.width * 1.6), int(art.height * 1.6)), Image.LANCZOS)
    x0 = 932                                         # puts the face at about x 1700, inside the desktop strip
    fade = Image.new("L", art.size, 255)
    fd = ImageDraw.Draw(fade)
    for x in range(900):                             # soft left edge into the dark
        fd.line([(x, 0), (x, art.height)], fill=int(255 * (x / 900) ** 2.2))
    im.paste(Image.merge("RGB", [art] * 3), (x0, 0), fade)

    d = ImageDraw.Draw(im)
    left, top = SAFE[0] + 60, SAFE[1] + 40
    im.paste(m := mark(92), (left, top), m)
    title = font("InterDisplay-Bold.otf", 104)
    d.text((left - 6, top + 110), "OOZ STUDIO INK", font=title, fill=PAPER)
    sub = font("Inter-SemiBold.otf", 32)
    d.text((left, top + 240), "C O M I C   A R T   ·   C H A R A C T E R   D E S I G N", font=sub, fill=GREY)
    tag = font("Inter-Bold.otf", 28)
    label = "NEW SHORTS MON · WED · FRI   |   LONG VIDEO FRIDAYS"
    tw = d.textlength(label, font=tag)
    d.rectangle([left, top + 298, left + tw + 40, top + 352], fill=RED)
    d.text((left + 20, top + 309), label, font=tag, fill=PAPER)
    im.save(OUT / "youtube-banner.png", optimize=True)
    return im


def profile():
    im = Image.new("RGB", (800, 800), BG)
    m = mark(560)
    im.paste(m, (120, 120), m)
    im.save(OUT / "youtube-profile.png", optimize=True)


if __name__ == "__main__":
    b = banner()
    profile()
    if "--guide" in sys.argv:                        # a check copy with the safe areas drawn, not for upload
        guide = b.copy()
        ImageDraw.Draw(guide).rectangle(SAFE, outline=(0, 255, 0), width=4)
        ImageDraw.Draw(guide).rectangle([0, 508, W, 931], outline=(255, 255, 0), width=4)
        guide.save(sys.argv[sys.argv.index("--guide") + 1])
