"""Regenerates the placeholder images: python3 tools/make_placeholders.py <ar-folder> (writes targets/target.png and assets/popup.png)."""
import math, random, sys
from PIL import Image, ImageDraw, ImageFont

out_dir = sys.argv[1]
random.seed(7)

def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)

# ---------- Target image (what the camera has to see) ----------
# Image tracking needs lots of distinct, high-contrast, non-repeating detail,
# so the sample target is a dense collage of letters and shapes.
W = H = 1000
img = Image.new("RGB", (W, H), (248, 246, 240))
d = ImageDraw.Draw(img)
palette = [(20, 20, 30), (200, 40, 60), (20, 110, 200), (240, 160, 20), (30, 150, 90), (120, 50, 160)]
chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
for i in range(170):
    kind = random.random()
    x, y = random.randint(40, W - 40), random.randint(40, H - 40)
    c = random.choice(palette)
    s = random.randint(18, 70)
    if kind < 0.45:
        f = font(s)
        d.text((x, y), random.choice(chars), fill=c, font=f, anchor="mm")
    elif kind < 0.65:
        r = s // 2
        d.ellipse((x - r, y - r, x + r, y + r), outline=c, width=random.randint(3, 8))
    elif kind < 0.85:
        n = random.randint(3, 6)
        rot = random.random() * math.tau
        pts = [(x + s * math.cos(rot + k * math.tau / n), y + s * math.sin(rot + k * math.tau / n)) for k in range(n)]
        d.polygon(pts, fill=c if random.random() < 0.5 else None, outline=c, width=4)
    else:
        a = random.random() * math.tau
        d.line((x, y, x + s * 1.5 * math.cos(a), y + s * 1.5 * math.sin(a)), fill=c, width=random.randint(4, 10))
# Title block
d.rectangle((60, 60, 940, 200), fill=(20, 20, 30))
d.text((500, 130), "shimushili AR", fill=(255, 255, 255), font=font(84), anchor="mm")
d.rectangle((60, 800, 940, 940), fill=(20, 20, 30))
d.text((500, 870), "scan me · ar.shimushili.com", fill=(255, 255, 255), font=font(54), anchor="mm")
d.rectangle((20, 20, W - 20, H - 20), outline=(20, 20, 30), width=10)
img.save(f"{out_dir}/targets/target.png", optimize=True)

# ---------- Pop-up image (what appears on top of the target) ----------
# Logo (assets/logo.png) on a rounded navy card, the Instagram name below it, then "click and follow".
NAME = "shimushi.li"
CTA = "click and follow"
NAVY = (11, 31, 46, 255)
W, H = 1000, 1380
pop = Image.new("RGBA", (W, H), (0, 0, 0, 0))
card = Image.new("RGBA", (W, H), NAVY)
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=150, fill=255)
pop.paste(card, (0, 0), mask)
logo = Image.open(f"{out_dir}/assets/logo.png").convert("RGBA")
logo = logo.resize((820, 820), Image.LANCZOS)
pop.alpha_composite(logo, ((W - 820) // 2, 40))
d = ImageDraw.Draw(pop)
# Instagram glyph + name on one line
name_font = font(118)
glyph = 100
name_w = d.textlength(NAME, font=name_font)
x0 = (W - (glyph + 34 + name_w)) / 2
gy = 1000
d.rounded_rectangle((x0, gy - glyph / 2, x0 + glyph, gy + glyph / 2), radius=28, outline=(255, 255, 255, 255), width=9)
d.ellipse((x0 + 27, gy - 23, x0 + glyph - 27, gy + 23), outline=(255, 255, 255, 255), width=9)
d.ellipse((x0 + glyph - 30, gy - glyph / 2 + 14, x0 + glyph - 18, gy - glyph / 2 + 26), fill=(255, 255, 255, 255))
d.text((x0 + glyph + 34, gy), NAME, fill=(255, 255, 255, 255), font=name_font, anchor="lm")
# CTA pill in the logo's orange→yellow
pill_font = font(64)
cta_w = d.textlength(CTA, font=pill_font)
px0, px1 = (W - cta_w) / 2 - 70, (W + cta_w) / 2 + 70
py0, py1 = 1150, 1290
pill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
pp = pill.load()
for y in range(int(py0), int(py1) + 1):
    for x in range(int(px0), int(px1) + 1):
        t = (x - px0) / max(1, px1 - px0)
        pp[x, y] = (int(255 * (1 - t) + 255 * t), int(94 * (1 - t) + 200 * t), int(20 * (1 - t) + 0 * t), 255)
pmask = Image.new("L", (W, H), 0)
ImageDraw.Draw(pmask).rounded_rectangle((px0, py0, px1, py1), radius=70, fill=255)
pop.paste(pill, (0, 0), pmask)
d.text((W / 2, (py0 + py1) / 2), CTA, fill=NAVY, font=pill_font, anchor="mm")
pop.save(f"{out_dir}/assets/popup.png", optimize=True)
print("ok")
