"""Regenerate the dashboard and plugin icons from the OpenPrintDeck SVG emblem.

Requires cairosvg and Pillow (``pip install cairosvg pillow``). Source artwork:
assets/openprintdeck-icon.svg (emblem) and assets/openprintdeck-logo.svg (emblem + wordmark).
"""

import io
import re
from pathlib import Path

import cairosvg
from PIL import Image


def render(svg, size):
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    emblem = (root / "assets" / "openprintdeck-icon.svg").read_text(encoding="utf-8")
    render(emblem, 1024).save(root / "assets" / "openprintdeck-icon.png", optimize=True)
    render(emblem, 192).save(root / "web" / "icon-192.png", optimize=True)
    render(emblem, 512).save(root / "orca_plugin" / "icon.png", optimize=True)

    # iOS and maskable icons: full-bleed background (the OS rounds or masks the corners).
    square = re.sub(r'<rect x="0.5" y="0.5"[^>]*/>', '<rect width="64" height="64" fill="url(#opd-bg)"/>', emblem)
    render(square, 180).convert("RGB").save(root / "web" / "apple-touch-icon.png", optimize=True)
    # Keep the artwork in the central safe area for adaptive app icon shapes.
    icon = render(square, 512).convert("RGB")
    art = render(square.replace('<rect width="64" height="64" fill="url(#opd-bg)"/>', ""), 400)
    icon.paste(art, (56, 56), art)
    icon.save(root / "web" / "icon-512.png", optimize=True)
