"""Twitch and Kick channel art in the Ooz Studio Ink look (dark, cream, red, the studio mark).

Run from the repo root:  python streaming/make_stream_art.py
  twitch-profile.png         800x800    profile picture (shown as a circle)
  twitch-banner.png          1200x480   profile banner (shown while you're offline)
  offline-banner.png         1920x1080  video player / offline banner (Twitch and Kick)
  twitch-panel-*.png         320x100    About, Commissions, Schedule, Socials panels
  kick-profile.png           512x512    profile picture (Kick asks for at least 128x128, under 4 MB)
  kick-banner.png            1280x700   profile banner (content kept central: Kick crops it differently by device)
"""
import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "streaming"
spec = importlib.util.spec_from_file_location("yt", ROOT / "youtube" / "make_youtube.py")
yt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yt)
BG, PAPER, RED, GREY, font, mark = yt.BG, yt.PAPER, yt.RED, yt.GREY, yt.font, yt.mark


def art_right(im: Image.Image, piece: str, x0: float, fade_w: float, scale: float, y_off: float = 0) -> None:
    """A sketchbook piece on the right, fading into the dark on its left edge."""
    w, h = im.size
    art = Image.open(ROOT / "portfolio" / piece).convert("L")
    k = h / art.height * scale
    k = max(k, (w - x0 + 2) / art.width)             # always reaches the right edge
    art = art.resize((int(art.width * k), int(art.height * k)), Image.LANCZOS)
    fade = Image.new("L", art.size, 255)
    fd = ImageDraw.Draw(fade)
    for x in range(int(fade_w)):
        fd.line([(x, 0), (x, art.height)], fill=int(255 * (x / fade_w) ** 2.2))
    im.paste(Image.merge("RGB", [art] * 3), (int(x0), int(y_off)), fade)


def tag(d, x, y, text, size):
    f = font("Inter-Bold.otf", size)
    tw = d.textlength(text, font=f)
    pad = size * 0.7
    d.rectangle([x, y, x + tw + 2 * pad, y + size * 1.9], fill=RED)
    d.text((x + pad, y + size * 0.42), text, font=f, fill=PAPER)


def brand_block(im, x, y, s, line2="C O M I C   A R T   ·   C H A R A C T E R   D E S I G N", tagline=None):
    """Mark, name, the small line and a red tag, scaled by s (1.0 = the YouTube banner's size)."""
    d = ImageDraw.Draw(im)
    m = mark(int(92 * s))
    im.paste(m, (int(x), int(y)), m)
    d.text((x - 6 * s, y + 110 * s), "OOZ STUDIO INK", font=font("InterDisplay-Bold.otf", int(104 * s)), fill=PAPER)
    d.text((x, y + 240 * s), line2, font=font("Inter-SemiBold.otf", int(32 * s)), fill=GREY)
    if tagline:
        tag(d, x, y + 298 * s, tagline, int(28 * s))


def profile(size: int, name: str):
    im = Image.new("RGB", (size, size), BG)
    m = mark(int(size * 0.7))
    im.paste(m, (int(size * 0.15), int(size * 0.15)), m)
    im.save(OUT / name, optimize=True)


def twitch_banner():
    im = Image.new("RGB", (1200, 480), BG)
    art_right(im, "01-samurai.jpg", 560, 360, 1.45, -150)
    brand_block(im, 56, 70, 0.78, tagline="LIVE INKING · COMMISSIONS OPEN")
    im.save(OUT / "twitch-banner.png", optimize=True)


def kick_banner():
    im = Image.new("RGB", (1280, 700), BG)
    art_right(im, "01-samurai.jpg", 600, 380, 1.15, -40)
    brand_block(im, 90, 190, 0.8, tagline="LIVE INKING · COMMISSIONS OPEN")
    im.save(OUT / "kick-banner.png", optimize=True)


def offline_banner():
    im = Image.new("RGB", (1920, 1080), BG)
    art_right(im, "05-the-clash.jpg", 1060, 420, 1.0)
    d = ImageDraw.Draw(im)
    m = mark(84)
    im.paste(m, (140, 120), m)
    d.text((244, 140), "OOZ STUDIO INK", font=font("InterDisplay-Bold.otf", 52), fill=PAPER)
    d.text((140, 330), "O F F L I N E   R I G H T   N O W", font=font("Inter-SemiBold.otf", 26), fill=GREY)
    d.text((132, 390), "SEE YOU AT", font=font("InterDisplay-Bold.otf", 130), fill=PAPER)
    d.text((132, 540), "THE NEXT STREAM", font=font("InterDisplay-Bold.otf", 130), fill=PAPER)
    d.rectangle([140, 735, 320, 741], fill=RED)
    body = font("Inter-SemiBold.otf", 30)
    d.text((140, 780), "Follow to get a ping when I go live.", font=body, fill=PAPER)
    d.text((140, 830), "New Shorts Mon · Wed · Fri  |  Long video Fridays, 12 PM EST on YouTube", font=body, fill=GREY)
    tag(d, 140, 900, "COMMISSIONS OPEN · OOZSTUDIOINK-DOTCOM.GITHUB.IO/OOZ-COMMISSION", 24)
    im.save(OUT / "offline-banner.png", optimize=True)


def panels():
    for slug, words in (("about", "ABOUT ME"), ("commissions", "COMMISSIONS"), ("schedule", "SCHEDULE"),
                        ("socials", "SOCIALS")):
        s = 4                                          # drawn 4x, then scaled down: crisp edges
        im = Image.new("RGB", (320 * s, 100 * s), BG)
        d = ImageDraw.Draw(im)
        m = mark(60 * s)
        im.paste(m, (20 * s, 20 * s), m)
        size = 34 * s
        while d.textlength(words, font=font("InterDisplay-Bold.otf", size)) > 204 * s:   # fits the panel
            size -= s
        d.text((98 * s, 22 * s + (34 * s - size) // 2), words, font=font("InterDisplay-Bold.otf", size), fill=PAPER)
        d.rectangle([98 * s, 68 * s, 150 * s, 72 * s], fill=RED)
        d.rectangle([0, 96 * s, 320 * s, 100 * s], fill=RED)
        im.resize((320, 100), Image.LANCZOS).save(OUT / f"twitch-panel-{slug}.png", optimize=True)


if __name__ == "__main__":
    profile(800, "twitch-profile.png")
    profile(512, "kick-profile.png")
    twitch_banner()
    kick_banner()
    offline_banner()
    panels()
    for p in sorted(OUT.glob("*.png")):
        print(p.name, Image.open(p).size, f"{p.stat().st_size / 1e6:.2f} MB")
