"""Turn a target image into a short .y4m clip that Chromium can use as a fake camera.

Usage: python3 make_fake_camera.py targets/target.png out.y4m
Then run headless_check.cjs (or Chrome with --use-file-for-fake-video-capture=out.y4m).
"""
import sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
W, H, FRAMES = 640, 480, 10

target = Image.open(src).convert("RGB")
scale = (H * 0.6) / max(target.size)
target = target.resize((max(1, int(target.width * scale)), max(1, int(target.height * scale))), Image.LANCZOS)
frame = Image.new("RGB", (W, H), (120, 125, 130))
frame.paste(target, ((W - target.width) // 2, (H - target.height) // 2))

ycc = frame.convert("YCbCr")
y, cb, cr = ycc.split()
cb = cb.resize((W // 2, H // 2), Image.BOX)
cr = cr.resize((W // 2, H // 2), Image.BOX)
payload = y.tobytes() + cb.tobytes() + cr.tobytes()

with open(dst, "wb") as f:
    f.write(f"YUV4MPEG2 W{W} H{H} F30:1 Ip A1:1 C420jpeg\n".encode())
    for _ in range(FRAMES):
        f.write(b"FRAME\n")
        f.write(payload)
print(f"wrote {dst}: {W}x{H}, {FRAMES} frames")
