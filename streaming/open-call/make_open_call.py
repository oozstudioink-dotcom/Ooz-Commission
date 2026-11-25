"""Profile banner for "Open Call for Artists" in the Ooz Studio Ink look.
Run from the repo root:  python streaming/open-call/make_open_call.py
  open-call-banner-1500x500.png   3:1, the usual profile-banner size
  open-call-banner-3000x1000.png  the same at 2x, for sites that ask for a large upload
"""
import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("art", HERE.parent / "make_stream_art.py")
art = importlib.util.module_from_spec(spec)
spec.loader.exec_module(art)
BG, PAPER, GREY, font, mark, tag, art_right = art.BG, art.PAPER, art.GREY, art.font, art.mark, art.tag, art.art_right


def banner(w: int, h: int) -> Path:
    s = w / 1500
    im = Image.new("RGB", (w, h), BG)
    art_right(im, "01-samurai.jpg", 760 * s, 330 * s, 1.55, -170 * s)       # his face, right side
    d = ImageDraw.Draw(im)
    m = mark(int(74 * s))
    im.paste(m, (int(70 * s), int(92 * s)), m)
    d.text((int(64 * s), int(180 * s)), "OOZ STUDIO INK", font=font("InterDisplay-Bold.otf", int(86 * s)), fill=PAPER)
    d.text((int(70 * s), int(292 * s)), "C O M I C   A R T   ·   C H A R A C T E R   D E S I G N",
           font=font("Inter-SemiBold.otf", int(22 * s)), fill=GREY)
    d.text((int(70 * s), int(328 * s)), "Drawn and inked by hand",
           font=font("Inter-SemiBold.otf", int(22 * s)), fill=PAPER)
    tag(d, int(70 * s), int(384 * s), "OPEN FOR COMMISSIONS · COLLABORATIONS", int(20 * s))
    out = HERE / f"open-call-banner-{w}x{h}.png"
    im.save(out, optimize=True)
    return out


if __name__ == "__main__":
    for size in ((1500, 500), (3000, 1000)):
        p = banner(*size)
        print(p.name, Image.open(p).size, f"{p.stat().st_size / 1e6:.2f} MB")
