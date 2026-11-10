"""QR codes for the Setup page, drawn as SVG. Standard library only.

Byte mode, error correction level M, versions 1 to 6 (up to 106 bytes: plenty for a dashboard
link). Follows ISO/IEC 18004; the module placement mirrors Project Nayuki's QR Code generator.
"""

# Level M, per version: (error correction codewords per block, data codewords per block, blocks)
_LEVEL_M = {1: (10, 16, 1), 2: (16, 28, 1), 3: (26, 44, 1), 4: (18, 32, 2), 5: (24, 43, 2), 6: (16, 27, 4)}
_ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP, _LOG = [0] * 512, [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i], _LOG[_value] = _value, _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]

_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def error_correction(data, count):
    """Reed-Solomon error correction codewords for one block."""
    generator = [1]
    for i in range(count):  # multiply by (x - a^i)
        generator = [a ^ _mul(b, _EXP[i]) for a, b in zip(generator + [0], [0] + generator)]
    rest = [0] * count
    for byte in data:
        factor = byte ^ rest[0]
        rest = rest[1:] + [0]
        for i in range(count):
            rest[i] ^= _mul(generator[i + 1], factor)
    return rest


def format_bits(mask):
    """15 format bits for level M (00) and this mask, with their BCH check bits."""
    rest = mask
    for _ in range(10):
        rest = (rest << 1) ^ ((rest >> 9) * 0x537)
    return ((mask << 10) | rest) ^ 0x5412


def _codewords(data):
    for version in range(1, 7):
        ec_len, block_len, blocks = _LEVEL_M[version]
        if len(data) <= block_len * blocks - 2:  # 4-bit mode + 8-bit length take 1.5 codewords
            break
    else:
        raise ValueError("text too long for this QR code (more than 106 bytes)")
    bits = "0100" + format(len(data), "08b") + "".join(format(b, "08b") for b in data)
    capacity = block_len * blocks * 8
    bits += "0" * min(4, capacity - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    words += [0xEC, 0x11] * ((block_len * blocks - len(words)) // 2 + 1)
    words = words[:block_len * blocks]
    split = [words[i * block_len:(i + 1) * block_len] for i in range(blocks)]
    ec = [error_correction(block, ec_len) for block in split]
    return version, ([b[i] for i in range(block_len) for b in split] + [b[i] for i in range(ec_len) for b in ec])


def _penalty(m):
    """Lower is easier to scan (the four penalty rules of the standard)."""
    size, score = len(m), 0
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, last = 0, None
        for value in line + [None]:
            if value == last:
                run += 1
                continue
            if run >= 5:
                score += run - 2
            run, last = 1, value
        text = "".join("1" if v else "0" for v in line)
        for pattern in ("10111010000", "00001011101"):  # looks like a finder pattern
            at = text.find(pattern)
            while at != -1:
                score += 40
                at = text.find(pattern, at + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    return score + abs(dark * 20 - size * size * 10) // (size * size) * 10


def encode(text):
    """The QR code for text: rows of booleans (True = dark), without the quiet zone."""
    version, words = _codewords(text.encode("utf-8"))
    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, value, grid=dark):  # x = column, y = row
        grid[y][x] = value
        fixed[y][x] = True

    def put_format(mask, grid):
        bits = format_bits(mask)
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):
            put(8, i, bit[i], grid)
        put(8, 7, bit[6], grid)
        put(8, 8, bit[7], grid)
        put(7, 8, bit[8], grid)
        for i in range(9, 15):
            put(14 - i, 8, bit[i], grid)
        for i in range(8):
            put(size - 1 - i, 8, bit[i], grid)
        for i in range(8, 15):
            put(8, size - 15 + i, bit[i], grid)
        put(8, size - 8, True, grid)  # the dark module

    for i in range(size):  # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder patterns and separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    centers = _ALIGNMENT[version]
    last = len(centers) - 1
    for i, ax in enumerate(centers):
        for j, ay in enumerate(centers):
            if (i, j) not in ((0, 0), (0, last), (last, 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    put_format(0, dark)  # reserve the format areas before placing data

    i, total = 0, len(words) * 8
    for right in range(size - 1, 0, -2):  # data in two-column zigzags, skipping the timing column
        if right <= 6:
            right -= 1
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not fixed[y][x] and i < total:
                    dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1

    best = None
    for mask, rule in enumerate(_MASKS):
        grid = [[v ^ (not fixed[y][x] and rule(x, y)) for x, v in enumerate(row)] for y, row in enumerate(dark)]
        put_format(mask, grid)
        score = _penalty(grid)
        if best is None or score < best[0]:
            best = (score, grid)
    return best[1]


def svg(text, border=4):
    """The QR code for text as an SVG image, with the white border scanners need."""
    m = encode(text)
    n = len(m) + 2 * border
    path = "".join("M%d %dh1v1h-1z" % (x + border, y + border)
                   for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges">'
            '<rect width="%d" height="%d" fill="#fff"/><path d="%s" fill="#000"/></svg>' % (n, n, n, n, path))
