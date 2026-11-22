"""Twitch and Kick channel art in the Ooz Studio Ink look (dark, cream, red, the studio mark).

Run from the repo root:  python streaming/make_stream_art.py
  twitch-profile.png         800x800    profile picture (shown as a circle)
  twitch-banner.png          1200x480   profile banner (shown while you're offline)
  offline-banner.png         1920x1080  video player / offline banner (Twitch and Kick)
  twitch-panel-*.png         320x100    About, Commissions, Schedule, Socials panels
  kick-profile.png           512x512    profile picture (Kick asks for at least 128x128, under 4 MB)
  kick-banner.png            1280x700   older Kick banner size
  kick-banner-3000x335.png   3000x335   Kick channel banner (the size Kick's upload box asks for)
  kick-offline-banner.png    1920x1080  Kick offline screen: everything centred, safe from Kick's zoom
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


def kick_wide_banner():
    """Kick's channel banner, 3000x335. The brand sits left of centre so narrower screens, which crop
    the sides, still show all of it."""
    im = Image.new("RGB", (3000, 335), BG)
    art_right(im, "01-samurai.jpg", 1880, 300, 3.4, -219)   # his face, not the hand and sword
    d = ImageDraw.Draw(im)
    m = mark(150)
    im.paste(m, (560, 92), m)
    d.text((750, 70), "OOZ STUDIO INK", font=font("InterDisplay-Bold.otf", 118), fill=PAPER)
    sub, sf = "C O M I C   A R T   \u00b7   C H A R A C T E R   D E S I G N", font("Inter-SemiBold.otf", 28)
    d.text((756, 214), sub, font=sf, fill=GREY)
    tag(d, 756 + d.textlength(sub, font=sf) + 36, 207, "LIVE INKING \u00b7 COMMISSIONS OPEN", 22)
    im.save(OUT / "kick-banner-3000x335.png", optimize=True)


def kick_offline_banner():
    """Kick zooms the offline banner to fill its player, cutting the edges. Here everything sits
    centred inside the middle ~70%, and the art is a full-bleed background, so a crop only trims art."""
    W_, H_ = 1920, 1080
    im = Image.new("RGB", (W_, H_), BG)
    art = Image.open(ROOT / "portfolio" / "05-the-clash.jpg").convert("L")
    k = max(W_ / art.width, H_ / art.height)
    art = art.resize((int(art.width * k), int(art.height * k)), Image.LANCZOS)
    art = art.crop(((art.width - W_) // 2, (art.height - H_) // 2, (art.width - W_) // 2 + W_, (art.height - H_) // 2 + H_))
    im = Image.blend(im, Image.merge("RGB", [art] * 3), 0.22)
    shade = Image.new("L", (W_, H_), 0)                  # darker in the middle, where the words are
    sd = ImageDraw.Draw(shade)
    for i in range(60):
        f = i / 60
        sd.ellipse([W_ * (0.5 - 0.62 + 0.4 * f), H_ * (0.5 - 0.7 + 0.45 * f),
                    W_ * (0.5 + 0.62 - 0.4 * f), H_ * (0.5 + 0.7 - 0.45 * f)], fill=int(150 * f))
    im.paste(Image.new("RGB", (W_, H_), BG), (0, 0), shade)
    d = ImageDraw.Draw(im)

    def centred(y, text, f, fill):
        d.text(((W_ - d.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)

    m = mark(96)
    name_f = font("InterDisplay-Bold.otf", 46)
    nw = d.textlength("OOZ STUDIO INK", font=name_f)
    x0 = (W_ - (96 + 22 + nw)) / 2
    im.paste(m, (int(x0), 190), m)
    d.text((x0 + 118, 214), "OOZ STUDIO INK", font=name_f, fill=PAPER)
    centred(340, "O F F L I N E   R I G H T   N O W", font("Inter-SemiBold.otf", 26), GREY)
    big = font("InterDisplay-Bold.otf", 112)
    centred(390, "SEE YOU AT", big, PAPER)
    centred(515, "THE NEXT STREAM", big, PAPER)
    d.rectangle([W_ / 2 - 80, 672, W_ / 2 + 80, 678], fill=RED)
    centred(706, "Follow to get a ping when I go live.", font("Inter-SemiBold.otf", 30), PAPER)
    centred(756, "New Shorts Mon \u00b7 Wed \u00b7 Fri  |  Long video Fridays, 12 PM EST on YouTube",
            font("Inter-SemiBold.otf", 26), GREY)
    label, tf = "COMMISSIONS OPEN \u00b7 OOZSTUDIOINK-DOTCOM.GITHUB.IO/OOZ-COMMISSION", 22
    tw = d.textlength(label, font=font("Inter-Bold.otf", tf))
    tag(d, (W_ - tw) / 2 - tf * 0.7, 822, label, tf)
    im.save(OUT / "kick-offline-banner.png", optimize=True)
    return im


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
    kick_wide_banner()
    offline_banner()
    kick_offline_banner()
    panels()
    for p in sorted(OUT.glob("*.png")):
        print(p.name, Image.open(p).size, f"{p.stat().st_size / 1e6:.2f} MB")
