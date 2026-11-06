"""Draw the OS_3DPMS app icons (camera lens over a print bed) with the standard library.

Run: python scripts/make_icons.py   (writes web/icon-192.png, web/icon-512.png, web/apple-touch-icon.png)
"""
import os
import struct
import zlib

BG = (15, 17, 21)
ACCENT = (79, 156, 255)
BED = (139, 145, 156)
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def color_at(x, y):
    """Colour at (x, y) in a 0..1 square. Everything stays inside the maskable safe zone."""
    dx, dy = x - 0.5, y - 0.45
    d2 = dx * dx + dy * dy
    if d2 < 0.085 ** 2:
        return ACCENT                                  # lens centre
    if 0.20 ** 2 < d2 < 0.26 ** 2:
        return ACCENT                                  # lens ring
    if 0.25 < x < 0.75 and 0.75 < y < 0.79:
        return BED                                     # print bed
    if (0.25 < x < 0.29 or 0.71 < x < 0.75) and 0.68 < y < 0.79:
        return BED                                     # bed supports
    return BG


def render(size, ss=4):
    rows = []
    n = ss * ss
    for py in range(size):
        row = bytearray(b"\x00")                       # PNG filter: none
        for px in range(size):
            r = g = b = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = color_at((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    r += c[0]
                    g += c[1]
                    b += c[2]
            row += bytes((r // n, g // n, b // n))
        rows.append(bytes(row))
    return b"".join(rows)


def png(size):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
        with open(os.path.join(WEB, name), "wb") as f:
            f.write(png(size))
        print("wrote", name)
