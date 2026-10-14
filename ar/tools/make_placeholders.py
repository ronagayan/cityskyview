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
S = 1000
pop = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# Instagram-like gradient card
grad = Image.new("RGBA", (S, S))
gp = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        r = int(254 * (1 - t) + 131 * t)
        g = int(218 * (1 - t) + 58 * t)
        b = int(117 * (1 - t) + 180 * t)
        gp[x, y] = (r, g, b, 255)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((20, 20, S - 20, S - 20), radius=140, fill=255)
pop.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(pop)
# camera glyph
cx, cy = S // 2, 400
d.rounded_rectangle((cx - 190, cy - 190, cx + 190, cy + 190), radius=90, outline=(255, 255, 255, 255), width=26)
d.ellipse((cx - 95, cy - 95, cx + 95, cy + 95), outline=(255, 255, 255, 255), width=26)
d.ellipse((cx + 105, cy - 135, cx + 145, cy - 95), fill=(255, 255, 255, 255))
d.text((S // 2, 700), "Follow us on", fill=(255, 255, 255, 255), font=font(70), anchor="mm")
d.text((S // 2, 800), "Instagram", fill=(255, 255, 255, 255), font=font(96), anchor="mm")
d.text((S // 2, 900), "tap to open", fill=(255, 255, 255, 220), font=font(44), anchor="mm")
pop.save(f"{out_dir}/assets/popup.png", optimize=True)
print("ok")
