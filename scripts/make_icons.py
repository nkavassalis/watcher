#!/usr/bin/env python3
"""Generate the icon set into <pkg>/static/ (Pillow only).

    .venv/bin/python scripts/make_icons.py

Rules this encodes (learned the hard way — see the pwa-home-screen skill):
  * favicon.png 48px  — transparent + small radius is fine (tab favicon fallback)
  * apple-touch-icon.png 180px — OPAQUE, SQUARE, no alpha, no rounded corners.
    iOS masks the icon itself, so alpha or radius gives a dark halo / double rounding.
  * icon-192.png / icon-512.png — manifest entries, opaque square.
  * favicon.svg is hand-authored and never touched here. Keep the colours below in
    sync with the CSS variables in static/style.css.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "watcher" / "static"
BG = (47, 111, 78, 255)        # --accent
FG = (247, 246, 243, 255)      # --bg (light)


def icon(size: int, *, transparent: bool, radius: int) -> Image.Image:
    img = Image.new("RGBA" if transparent else "RGB", (size, size))
    d = ImageDraw.Draw(img)
    if transparent and radius:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BG)
    else:
        d.rectangle((0, 0, size - 1, size - 1), fill=BG)
    m = size * 0.28
    d.ellipse((m, m, size - m, size - m), outline=FG, width=max(2, size // 13))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(48, transparent=True, radius=10).save(OUT / "favicon.png")
    icon(180, transparent=False, radius=0).save(OUT / "apple-touch-icon.png")
    icon(192, transparent=False, radius=0).save(OUT / "icon-192.png")
    icon(512, transparent=False, radius=0).save(OUT / "icon-512.png")
    print("wrote favicon.png, apple-touch-icon.png, icon-192.png, icon-512.png ->", OUT)


if __name__ == "__main__":
    main()
