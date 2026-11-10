"""Regenerate the dashboard icons from the README artwork (requires Pillow)."""

from pathlib import Path

from PIL import Image


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    with Image.open(root / "assets" / "os3dpms-logo.png") as artwork:
        # Use the printer emblem by itself so it is legible in the browser tab.
        emblem = artwork.convert("RGB").crop((193, 38, 1061, 906))
    for name, size in (("icon-192.png", 192), ("apple-touch-icon.png", 180)):
        emblem.resize((size, size), Image.Resampling.LANCZOS).save(root / "web" / name)

    # Keep the emblem in the central safe area for adaptive app icon shapes.
    icon = Image.new("RGB", (512, 512), emblem.getpixel((0, 0)))
    icon.paste(emblem.resize((384, 384), Image.Resampling.LANCZOS), (64, 64))
    icon.save(root / "web" / "icon-512.png")
