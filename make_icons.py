"""
Generate Alexa Flash Briefing icons (108x108 and 512x512 PNGs).

A simple "sunrise over the horizon" mark on a navy-to-amber sky — a clean
'morning brief' motif. Drawn at 4x and downsampled for smooth edges.

    python make_icons.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "icons"
OUT.mkdir(exist_ok=True)

SKY_TOP = (16, 38, 76)      # deep navy
SKY_BOT = (242, 153, 74)    # warm amber
GROUND = (12, 26, 52)       # darker navy
SUN = (255, 214, 107)       # warm yellow
RAY = (255, 226, 150)
WHITE = (245, 247, 250)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _load_font(px):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, px)
        except Exception:  # noqa: BLE001
            continue
    return None


def make_icon(size, with_text):
    scale = 4
    s = size * scale
    img = Image.new("RGB", (s, s))
    d = ImageDraw.Draw(img)

    # Sky gradient
    for y in range(s):
        d.line([(0, y), (s, y)], fill=_lerp(SKY_TOP, SKY_BOT, y / s))

    ground_y = int(s * 0.70)
    cx, cy = s // 2, ground_y
    sun_r = int(s * 0.21)

    # Sun rays (drawn first so the sun disc sits on top)
    ray_len = int(s * 0.11)
    ray_gap = int(s * 0.05)
    for ang in range(-75, 76, 25):
        a = math.radians(ang - 90)  # fan upward
        x1 = cx + math.cos(a) * (sun_r + ray_gap)
        y1 = cy + math.sin(a) * (sun_r + ray_gap)
        x2 = cx + math.cos(a) * (sun_r + ray_gap + ray_len)
        y2 = cy + math.sin(a) * (sun_r + ray_gap + ray_len)
        d.line([(x1, y1), (x2, y2)], fill=RAY, width=max(2, int(s * 0.012)))

    # Sun disc
    d.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r], fill=SUN)

    # Ground band covers the lower half of the sun -> "rising" effect
    d.rectangle([0, ground_y, s, s], fill=GROUND)

    if with_text:
        font = _load_font(int(s * 0.085))
        if font:
            txt = "DAILY BRIEF"
            bbox = d.textbbox((0, 0), txt, font=font)
            tw = bbox[2] - bbox[0]
            d.text(((s - tw) / 2, s * 0.80), txt, font=font, fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


for size, with_text in ((512, True), (108, False)):
    path = OUT / "icon-{}.png".format(size)
    make_icon(size, with_text).save(path, "PNG")
    print("wrote", path)
