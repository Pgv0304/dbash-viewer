"""Draws the application icon (green ASH grid with a rising activity chart) as PNG, ICNS (macOS) and ICO (Windows)."""
from __future__ import annotations
import os
import subprocess
import sys
from PIL import Image, ImageDraw

out = sys.argv[1] if len(sys.argv) > 1 else "desktop/icon"
os.makedirs(out, exist_ok=True)
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=(13, 19, 26, 255))
# stacked activity bands in wait-class colours
import math
cols = [(0, 204, 0), (0, 74, 231), (228, 104, 0), (192, 40, 0)]
x0, x1, yb = 140, S - 140, S - 210
base = [0.0] * 41
for li, c in enumerate(cols):
    top = [base[i] + max(18, 70 + 55 * math.sin(i * 0.55 + li) + 30 * math.sin(i * 0.23 + li * 2)) * (1.0 - li * 0.18) for i in range(41)]
    pts = [(x0 + (x1 - x0) * i / 40, yb - top[i]) for i in range(41)] + [(x0 + (x1 - x0) * i / 40, yb - base[i]) for i in range(40, -1, -1)]
    d.polygon(pts, fill=c + (255,))
    base = top
# grid mark (the app's ▦ logo) top-left
g0, gs = 150, 46
for r in range(4):
    for k in range(4):
        d.rectangle((g0 + k * gs, g0 + r * gs, g0 + k * gs + gs - 10, g0 + r * gs + gs - 10), fill=(63, 185, 80, 255))
d.line((x0, yb, x1, yb), fill=(180, 190, 200, 255), width=6)
png = os.path.join(out, "icon.png"); im.save(png)
im.save(os.path.join(out, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
if sys.platform == "darwin":
    iconset = os.path.join(out, "icon.iconset"); os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        im.resize((size, size), Image.LANCZOS).save(os.path.join(iconset, f"icon_{size}x{size}.png"))
        im.resize((size * 2, size * 2), Image.LANCZOS).save(os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(out, "icon.icns")], check=True)
print("icons in", out)
