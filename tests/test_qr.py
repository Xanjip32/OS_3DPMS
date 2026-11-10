"""The Setup page QR code: published reference values, and reading a generated code back."""
import unittest

from dashboard import qr

URL = "https://os3dpms.tail464ff2.ts.net/"


def read_back(m):
    """Decode a level-M, byte-mode code made by qr.encode (format, unmask, zigzag, blocks, text)."""
    size = len(m)
    version = (size - 17) // 4
    bits = sum(int(m[i][8]) << i for i in range(6)) | int(m[7][8]) << 6 | int(m[8][8]) << 7 | int(m[8][7]) << 8
    bits |= sum(int(m[8][14 - i]) << i for i in range(9, 15))
    mask = next(k for k in range(8) if qr.format_bits(k) == bits)
    # Modules that carry data: all but finders with their format areas, timing lines and alignment.
    function = [[(x < 9 and y < 9) or (x > size - 9 and y < 9) or (x < 9 and y > size - 9) or 6 in (x, y)
                 for x in range(size)] for y in range(size)]
    if version > 1:
        a = 4 * version + 10  # the one alignment pattern of versions 2 to 6
        for y in range(a - 2, a + 3):
            for x in range(a - 2, a + 3):
                function[y][x] = True
    rule = qr._MASKS[mask]
    out = []
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not function[y][x]:
                    out.append(m[y][x] ^ rule(x, y))
    ec_len, block_len, blocks = qr._LEVEL_M[version]
    words = [int("".join("1" if b else "0" for b in out[i:i + 8]), 2) for i in range(0, (block_len + ec_len) * blocks * 8, 8)]
    data = [words[i * blocks + b] for b in range(blocks) for i in range(block_len)]
    ec = [words[block_len * blocks + i * blocks + b] for b in range(blocks) for i in range(ec_len)]
    for b in range(blocks):  # every block's correction codewords match its data
        assert qr.error_correction(data[b * block_len:(b + 1) * block_len], ec_len) == ec[b * ec_len:(b + 1) * ec_len]
    stream = "".join(format(w, "08b") for w in data)
    assert stream[:4] == "0100", "byte mode"
    length = int(stream[4:12], 2)
    return bytes(int(stream[12 + 8 * i:20 + 8 * i], 2) for i in range(length)).decode()


class QrTests(unittest.TestCase):
    def test_reed_solomon_matches_the_standard_example(self):
        # "HELLO WORLD", version 1-M (the worked example in the QR standard and its tutorials)
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.error_correction(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_format_bits_match_the_published_table(self):
        table = ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
                 "100010111111001", "100000011001110", "100111110010111", "100101010100000"]
        self.assertEqual([format(qr.format_bits(mask), "015b") for mask in range(8)], table)

    def test_link_reads_back_and_has_the_fixed_patterns(self):
        for text in (URL, "https://a.ts.net/", "https://" + "x" * 90 + ".ts.net/"):
            m = qr.encode(text)
            size = len(m)
            for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder centers are dark, ring light
                self.assertTrue(m[cy][cx])
                self.assertFalse(m[cy - 2][cx])
            self.assertTrue(m[size - 8][8])  # the dark module
            self.assertEqual([m[6][x] for x in range(8, size - 8)], [x % 2 == 0 for x in range(8, size - 8)])
            self.assertEqual(read_back(m), text)
        self.assertEqual(len(qr.encode(URL)), 29)  # 34 bytes: version 3

    def test_too_long_is_refused_and_svg_has_a_border(self):
        with self.assertRaises(ValueError):
            qr.encode("x" * 107)
        self.assertIn('viewBox="0 0 37 37"', qr.svg(URL))


if __name__ == "__main__":
    unittest.main()
