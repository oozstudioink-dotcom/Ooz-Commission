"""Builds the Reddit commission sheet (reddit/commission-sheet.png) from reddit/sheet/plan.json.
Run from the repository root: python3 reddit/make_sheet.py"""
import json
from PIL import Image, ImageDraw, ImageFont

D = json.load(open('reddit/sheet/plan.json'))
F = '/usr/share/fonts/opentype/inter/'
def font(w, s): return ImageFont.truetype(F + f'Inter-{w}.otf', s)
INK, PAPER, RED, GREY, LINE = (20, 20, 20), (247, 246, 243), (192, 57, 43), (105, 105, 105), (212, 209, 201)
W, M, TH = 2000, 90, 560                      # width, side margin, example height

def spaced(d, xy, text, f, fill, sp):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill); x += d.textlength(ch, font=f) + sp
    return x

def card_height(p):
    return 150 + (TH + 30 if p['images'] else 0) + (50 if p.get('caption') else 0) + 30

TERMS = [("50% deposit", "to start; the rest on delivery"),
         ("2 revisions", "included with every package"),
         ("High-res PNG", "delivered by Google Drive link"),
         ("Personal use", "included; commercial rights +30–50%"),
         ("Rush", "+25–50% · extra character +50%"),
         ("No NSFW", "or work outside my boundaries")]
head_h, terms_h, foot_h = 470, 120 + 2 * 120 + 40, 230
H = head_h + sum(card_height(p) + 40 for p in D['packages']) + terms_h + foot_h
img = Image.new('RGB', (W, H), PAPER); d = ImageDraw.Draw(img)

# header
d.rectangle((0, 0, W, head_h), fill=INK)
cx, cy, r = W // 2, 120, 46
d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=PAPER, width=4); d.line((cx - 32, cy, cx + 32, cy), fill=PAPER, width=3)
for fy in (-36, -18, 11, 29): d.ellipse((cx - 5, cy + fy - 5, cx + 5, cy + fy + 5), fill=RED)
t = 'OOZ STUDIO INK'; f = font('Bold', 110); d.text(((W - d.textlength(t, font=f)) / 2, 190), t, font=f, fill=PAPER)
s = 'COMIC ART  ·  CHARACTER DESIGN  ·  CONCEPT ART'; f = font('SemiBold', 32)
sw = sum(d.textlength(c, font=f) + 5 for c in s); spaced(d, ((W - sw) / 2, 330), s, f, (190, 188, 182), 5)
lab = 'COMMISSIONS OPEN'; f = font('Bold', 30); lw = sum(d.textlength(c, font=f) + 5 for c in lab) + 40
d.rectangle(((W - lw) / 2, 392, (W + lw) / 2, 444), fill=RED); spaced(d, ((W - lw) / 2 + 20, 400), lab, f, (255, 255, 255), 5)

# packages
y = head_h + 50
for k, p in enumerate(D['packages'], 1):
    h = card_height(p)
    d.rectangle((M, y, W - M, y + h), fill=(255, 255, 255), outline=LINE, width=2)
    d.line((M, y, M + 26, y), fill=RED, width=5); d.line((M, y, M, y + 26), fill=RED, width=5)
    d.text((M + 40, y + 34), f"{k}. {p['name']}", font=font('Bold', 52), fill=INK)
    f = font('Bold', 44); d.text((W - M - 40 - d.textlength(p['price'], font=f), y + 40), p['price'], font=f, fill=RED)
    sub = p['desc'] + (f"  ·  {p['turnaround']}" if p['turnaround'] else '')
    d.text((M + 40, y + 102), sub, font=font('Regular', 32), fill=GREY)
    yy = y + 160
    if p['images']:
        ims = [Image.open('reddit/sheet/' + f).convert('RGB') for f in p['images']]
        ims = [im.resize((round(im.width * TH / im.height), TH), Image.LANCZOS) for im in ims]
        gap, total = 30, sum(im.width for im in ims) + 30 * (len(ims) - 1)
        avail = W - 2 * M - 80
        if total > avail:                                  # shrink to fit the card
            s_ = (avail - gap * (len(ims) - 1)) / sum(im.width for im in ims)
            ims = [im.resize((round(im.width * s_), round(TH * s_)), Image.LANCZOS) for im in ims]
            total = avail
        x = (W - total) // 2
        for im in ims:
            img.paste(im, (x, yy)); d.rectangle((x - 1, yy - 1, x + im.width, yy + im.height), outline=LINE, width=2)
            x += im.width + gap
        yy += TH + 24
    if p.get('caption'):
        f = font('Medium', 28); d.text(((W - d.textlength(p['caption'], font=f)) / 2, yy), p['caption'], font=f, fill=GREY)
    y += h + 40

# terms
y += 10
d.text((M, y), 'GOOD TO KNOW', font=font('Bold', 34), fill=GREY); y += 70
cw = (W - 2 * M) // 3
for i, (a, b) in enumerate(TERMS):
    x, yy = M + (i % 3) * cw, y + (i // 3) * 120
    d.rectangle((x, yy + 6, x + 6, yy + 80), fill=RED)
    d.text((x + 26, yy), a, font=font('Bold', 36), fill=INK); d.text((x + 26, yy + 48), b, font=font('Regular', 26), fill=GREY)
y += 2 * 120 + 40

# footer
d.rectangle((0, H - foot_h, W, H), fill=INK)
a = 'DM me here on Reddit to start'; f = font('Bold', 52); d.text(((W - d.textlength(a, font=f)) / 2, H - foot_h + 50), a, font=f, fill=PAPER)
b = 'oozstudioink-dotcom.github.io/Ooz-Commission'; f = font('Medium', 34)
d.text(((W - d.textlength(b, font=f)) / 2, H - foot_h + 130), b, font=f, fill=(190, 188, 182))
img.save('reddit/commission-sheet.png', optimize=True)
img.convert('RGB').save('reddit/commission-sheet.jpg', quality=90)
print(img.size)
