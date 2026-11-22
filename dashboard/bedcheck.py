"""Bed-clear check for the print queue: compare the camera picture with an empty-bed picture.

No AI and no extra packages. Each printer can have a reference picture of its empty bed,
taken from the dashboard. After a print, the current camera picture is compared with it:

1. Both JPEGs are turned into a small grayscale picture. Without extra packages this reads
   only the DC (average) value of every 8x8 block of a baseline JPEG, which gives a 1/8 scale
   grayscale image without a full decode. When Pillow is installed it is used instead
   (faster, and it also reads progressive JPEGs).
2. The region of interest (ROI: the bed, as fractions of the camera picture) is cut out and
   averaged into at most 32 x 24 cells.
3. Overall brightness is matched (lights and camera exposure drift during the day).
4. A cell counts as changed when it differs by more than `tolerance` gray levels. The score
   is the percentage of changed cells; the bed counts as clear when score <= threshold.

The queue only starts a job after several clear pictures in a row (see web.AutoStarter).
Every error here means "not clear": the caller never starts a print on an exception.
"""
import hashlib
import io
import json
import logging
import os
import threading
import time

log = logging.getLogger("dashboard.bedcheck")

GRID = (32, 24)               # cells across and down the ROI (fewer when the picture is small)
MAX_BLOCKS = 160_000          # about 2560 x 2048 pixels: bigger pictures are refused (too slow)
MIN_BRIGHTNESS = 12           # average gray level: darker means the light is off
MAX_GAIN = 2.0                # brightness may change by up to x2 between reference and now
COLOR_WEIGHT = 1.5            # color differences are smaller than brightness ones: weigh them up

DEFAULTS = {
    "auto_start": False,
    "bed_check_threshold": 0.5,       # percent of changed cells that still counts as clear
    "bed_check_tolerance": 24,        # gray levels (0-255) a cell may differ before it counts
    "bed_check_frames": 3,            # clear pictures in a row before a start
    "bed_check_roi": [0.0, 0.0, 1.0, 1.0],
    "bed_check_require_removal": True,  # a part must have been seen since the last print
}


class BedCheckError(ValueError):
    """The picture cannot be checked (format, size, darkness...). Means: do not start."""


# ---------------------------------------------------------------- settings

def settings(p):
    """Bed-check settings of a validated printer entry, with defaults."""
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in p:
            out[key] = p[key]
    return out


def validate_printer(p, pid):
    """Check and normalize the bed-check fields of one printer entry (config.validate)."""
    from .config import ConfigError
    p["auto_start"] = bool(p.get("auto_start", False))
    try:
        threshold = float(p.get("bed_check_threshold", DEFAULTS["bed_check_threshold"]))
        tolerance = int(p.get("bed_check_tolerance", DEFAULTS["bed_check_tolerance"]))
        frames = int(p.get("bed_check_frames", DEFAULTS["bed_check_frames"]))
    except (TypeError, ValueError):
        raise ConfigError("%s: bed check settings must be numbers" % pid) from None
    if not 0.1 <= threshold <= 50:
        raise ConfigError("%s: bed_check_threshold must be between 0.1 and 50 (percent)" % pid)
    if not 4 <= tolerance <= 128:
        raise ConfigError("%s: bed_check_tolerance must be between 4 and 128" % pid)
    if not 1 <= frames <= 10:
        raise ConfigError("%s: bed_check_frames must be between 1 and 10" % pid)
    roi = p.get("bed_check_roi", DEFAULTS["bed_check_roi"])
    if isinstance(roi, str):
        roi = [x for x in roi.replace(";", ",").split(",") if x.strip()]
    try:
        roi = [float(x) for x in roi]
    except (TypeError, ValueError):
        raise ConfigError("%s: bed_check_roi must be four numbers: left, top, right, bottom" % pid) from None
    if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1) \
            or roi[2] - roi[0] < 0.05 or roi[3] - roi[1] < 0.05:
        raise ConfigError("%s: bed_check_roi must be left, top, right, bottom between 0 and 1 "
                          "(for example 0, 0.3, 1, 1)" % pid)
    p["bed_check_threshold"] = round(threshold, 2)
    p["bed_check_tolerance"] = tolerance
    p["bed_check_frames"] = frames
    p["bed_check_roi"] = [round(x, 3) for x in roi]
    p["bed_check_require_removal"] = bool(p.get("bed_check_require_removal", True))


# ---------------------------------------------------------------- JPEG to small grayscale

def _pillow():
    try:
        from PIL import Image  # optional: faster, and reads progressive JPEGs
        return Image
    except Exception:
        return None


def backend():
    return "pillow" if _pillow() is not None else "builtin"


def gray_grid(jpeg, use_pillow=None):
    """(width, height, Y, Cb, Cr): a 1/8 scale picture of a JPEG, one list (0-255) per channel.

    Brightness alone misses parts whose color differs from the bed but not their brightness
    (a red part on a gray bed), so the color channels are compared too."""
    if not isinstance(jpeg, (bytes, bytearray)) or jpeg[:2] != b"\xff\xd8":
        raise BedCheckError("not a JPEG picture")
    image = _pillow() if use_pillow in (None, True) else None
    if image is not None:
        try:
            with image.open(io.BytesIO(jpeg)) as im:
                w, h = im.size
                if (w // 8 + 1) * (h // 8 + 1) > MAX_BLOCKS * 2:
                    raise BedCheckError("camera picture is too large to check (%dx%d)" % (w, h))
                im.draft("YCbCr", ((w + 7) // 8, (h + 7) // 8))  # JPEG: decode at reduced size
                small = im.convert("YCbCr").resize(((w + 7) // 8, (h + 7) // 8), image.BOX)
                y, cb, cr = (list(band.tobytes()) for band in small.split())
                return small.size[0], small.size[1], y, cb, cr
        except BedCheckError:
            raise
        except Exception as e:
            if use_pillow:
                raise BedCheckError("Pillow could not read the picture: %s" % e) from None
            # fall through to the built-in reader
    return decode_dc(jpeg)


def _build_lut(counts, symbols):
    """16-bit prefix -> (symbol << 8 | code length); 0 marks an invalid code."""
    lut = [0] * 65536
    code, k = 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if k >= len(symbols):
                raise BedCheckError("bad Huffman table")
            span = 1 << (16 - length)
            start = code << (16 - length)
            if start + span > 65536:
                raise BedCheckError("bad Huffman table")
            lut[start:start + span] = [(symbols[k] << 8) | length] * span
            code += 1
            k += 1
        code <<= 1
    return lut


def _entropy_segments(data, pos):
    """Unstuffed entropy-coded data from pos, split at restart markers. Returns (segments, end)."""
    segments, cur = [], bytearray()
    n = len(data)
    while True:
        j = data.find(b"\xff", pos)
        if j < 0 or j + 1 >= n:
            cur += data[pos:]
            segments.append(bytes(cur))
            return segments, n
        cur += data[pos:j]
        m = data[j + 1]
        if m == 0x00:
            cur.append(0xFF)
            pos = j + 2
        elif m == 0xFF:
            pos = j + 1          # fill byte
        elif 0xD0 <= m <= 0xD7:
            segments.append(bytes(cur))
            cur = bytearray()
            pos = j + 2
        else:
            segments.append(bytes(cur))
            return segments, j


def decode_dc(jpeg):
    """Built-in reader: block averages (DC values) of every component of a baseline JPEG."""
    data = bytes(jpeg)
    pos, n = 2, len(data)
    qdc = {}
    dc_tables, ac_tables = {}, {}
    frame = None
    restart = 0
    while pos < n:
        if data[pos] != 0xFF:
            raise BedCheckError("damaged JPEG (marker expected)")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > n:
            raise BedCheckError("damaged JPEG (truncated)")
        length = (data[pos] << 8) | data[pos + 1]
        seg = data[pos + 2:pos + length]
        if len(seg) != length - 2:
            raise BedCheckError("damaged JPEG (truncated segment)")
        pos += length
        if marker == 0xDB:                      # quantization tables: keep the DC entry
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                if pq:
                    qdc[tq] = (seg[i + 1] << 8) | seg[i + 2]
                    i += 129
                else:
                    qdc[tq] = seg[i + 1]
                    i += 65
        elif marker == 0xC4:                    # Huffman tables
            i = 0
            while i < len(seg):
                tc, th = seg[i] >> 4, seg[i] & 15
                counts = list(seg[i + 1:i + 17])
                total = sum(counts)
                symbols = list(seg[i + 17:i + 17 + total])
                (ac_tables if tc else dc_tables)[th] = _build_lut(counts, symbols)
                i += 17 + total
        elif marker == 0xDD:
            restart = (seg[0] << 8) | seg[1]
        elif marker in (0xC0, 0xC1):
            height, width, ncomp = (seg[1] << 8) | seg[2], (seg[3] << 8) | seg[4], seg[5]
            if not width or not height:
                raise BedCheckError("JPEG without a size (DNL) is not supported")
            comps = []
            for c in range(ncomp):
                cid, hv, tq = seg[6 + 3 * c], seg[7 + 3 * c], seg[8 + 3 * c]
                comps.append({"id": cid, "h": hv >> 4, "v": hv & 15, "tq": tq})
            frame = {"w": width, "h": height, "comps": comps}
        elif 0xC2 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            raise BedCheckError("this camera sends progressive or other non-baseline JPEGs: "
                                "install Pillow (py -m pip install pillow) to check them")
        elif marker == 0xDA:
            if frame is None:
                raise BedCheckError("damaged JPEG (scan before frame)")
            ns = seg[0]
            scan = [(seg[1 + 2 * i], seg[2 + 2 * i] >> 4, seg[2 + 2 * i] & 15) for i in range(ns)]
            segments, pos = _entropy_segments(data, pos)
            if len(scan) != len(frame["comps"]):
                raise BedCheckError("JPEGs with one scan per color are not supported: install Pillow")
            return _decode_scan(frame, scan, segments, restart, qdc, dc_tables, ac_tables)
    raise BedCheckError("damaged JPEG (no picture data)")


def _ceil_div(a, b):
    return (a + b - 1) // b


def _decode_scan(frame, scan, segments, restart, qdc, dc_tables, ac_tables):
    comps = {c["id"]: c for c in frame["comps"]}
    hmax = max(c["h"] for c in frame["comps"])
    vmax = max(c["v"] for c in frame["comps"])
    y = frame["comps"][0]
    yw = _ceil_div(_ceil_div(frame["w"] * y["h"], hmax), 8)    # luminance blocks across
    yh = _ceil_div(_ceil_div(frame["h"] * y["v"], vmax), 8)    # and down
    if yw * yh > MAX_BLOCKS:
        raise BedCheckError("camera picture is too large to check (%dx%d)" % (frame["w"], frame["h"]))
    plan = []
    for cid, td, ta in scan:
        c = comps.get(cid)
        if c is None or td not in dc_tables or ta not in ac_tables or c["tq"] not in qdc:
            raise BedCheckError("damaged JPEG (tables missing)")
        plan.append((c["h"], c["v"], dc_tables[td], ac_tables[ta], qdc[c["tq"]]))
    if len(plan) == 1:          # one component (grayscale): one block per MCU, in raster order
        mcux, mcuy = yw, yh
        plan = [(1, 1) + plan[0][2:]]
    else:
        mcux = _ceil_div(frame["w"], 8 * hmax)
        mcuy = _ceil_div(frame["h"], 8 * vmax)
    widths = [mcux * c[0] for c in plan]
    grids = [[0.0] * (w * mcuy * c[1]) for w, c in zip(widths, plan)]
    seg_i = 0
    d = segments[0] + b"\x00\x00\x00\x00"
    limit = (len(d) - 4) * 8 + 64
    bit = 0
    preds = [0] * len(plan)
    for m in range(mcux * mcuy):
        if restart and m and m % restart == 0:
            seg_i += 1
            if seg_i >= len(segments):
                raise BedCheckError("damaged JPEG (restart markers missing)")
            d = segments[seg_i] + b"\x00\x00\x00\x00"
            limit = (len(d) - 4) * 8 + 64
            bit = 0
            preds = [0] * len(plan)
        mx, my = m % mcux, m // mcux
        for ci, (ch, cv, dc_lut, ac_lut, q0) in enumerate(plan):
            grid, gw = grids[ci], widths[ci]
            for by in range(cv):
                for bx in range(ch):
                    # DC coefficient
                    i = bit >> 3
                    w = ((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]) >> (8 - (bit & 7)) & 0xFFFF
                    e = dc_lut[w]
                    if not e:
                        raise BedCheckError("damaged JPEG (bad DC code)")
                    bit += e & 255
                    s = e >> 8
                    if s:
                        i = bit >> 3
                        w = ((d[i] << 16) | (d[i + 1] << 8) | d[i + 2])
                        diff = (w >> (24 - (bit & 7) - s)) & ((1 << s) - 1)
                        if diff < (1 << (s - 1)):
                            diff -= (1 << s) - 1
                        bit += s
                        preds[ci] += diff
                    # AC coefficients: decoded only to skip them
                    k = 1
                    while k < 64:
                        i = bit >> 3
                        w = ((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]) >> (8 - (bit & 7)) & 0xFFFF
                        e = ac_lut[w]
                        if not e:
                            raise BedCheckError("damaged JPEG (bad AC code)")
                        bit += e & 255
                        rs = e >> 8
                        size = rs & 15
                        if size:
                            bit += size
                            k += (rs >> 4) + 1
                        elif rs == 0xF0:
                            k += 16
                        else:
                            break
                    if bit > limit:
                        raise BedCheckError("damaged JPEG (picture data cut short)")
                    grid[(my * cv + by) * gw + mx * ch + bx] = preds[ci] * q0 / 8.0 + 128.0
    # Every channel at the luminance block grid (color is usually stored at half size).
    out = []
    yh_, yv_ = plan[0][0], plan[0][1]
    for ci, (ch, cv) in enumerate((c[0], c[1]) for c in plan):
        grid, gw = grids[ci], widths[ci]
        out.append([min(255.0, max(0.0, grid[(gy * cv // yv_) * gw + gx * ch // yh_]))
                    for gy in range(yh) for gx in range(yw)])
    while len(out) < 3:
        out.append([128.0] * (yw * yh))    # grayscale picture: no color
    return (yw, yh) + tuple(out[:3])


# ---------------------------------------------------------------- comparison

def cells(grid, roi):
    """Average every channel of a grid over the ROI into at most GRID cells.

    Returns (cw, ch, Y, Cb, Cr) with one list per channel."""
    w, h = grid[0], grid[1]
    x0, y0 = int(roi[0] * w), int(roi[1] * h)
    x1, y1 = max(x0 + 1, int(round(roi[2] * w))), max(y0 + 1, int(round(roi[3] * h)))
    rw, rh = x1 - x0, y1 - y0
    cw, ch = min(GRID[0], rw), min(GRID[1], rh)
    index = []
    for yy in range(y0, y1):
        cy = (yy - y0) * ch // rh
        index += [(yy * w + xx, cy * cw + (xx - x0) * cw // rw) for xx in range(x0, x1)]
    counts = [0] * (cw * ch)
    for _, c in index:
        counts[c] += 1
    out = [cw, ch]
    for values in grid[2:5]:
        sums = [0.0] * (cw * ch)
        for src, c in index:
            sums[c] += values[src]
        out.append([s / max(1, k) for s, k in zip(sums, counts)])
    return tuple(out)


def compare(reference, current, roi=None, tolerance=24, threshold=0.5):
    """Compare two grids (from gray_grid). Returns a result dict; raises BedCheckError.

    Brightness is matched by its overall gain (light, exposure); color by its overall offset
    (white balance). A cell has changed when its brightness or its color differs by more than
    `tolerance` after that."""
    roi = roi or DEFAULTS["bed_check_roi"]
    if reference[:2] != current[:2]:
        raise BedCheckError("the camera picture size changed (%dx%d blocks, reference %dx%d): "
                            "save a new empty-bed picture" % (current[0], current[1], reference[0], reference[1]))
    rw, rh, ref_y, ref_cb, ref_cr = cells(reference, roi)
    _, _, cur_y, cur_cb, cur_cr = cells(current, roi)
    n = len(ref_y)
    mean = lambda v: sum(v) / n  # noqa: E731
    ref_mean, cur_mean = mean(ref_y), mean(cur_y)
    if cur_mean < MIN_BRIGHTNESS:
        raise BedCheckError("the camera picture is too dark (is the light off?)")
    if ref_mean < MIN_BRIGHTNESS:
        raise BedCheckError("the empty-bed picture is too dark: save a new one with the light on")
    gain = ref_mean / cur_mean
    if not 1 / MAX_GAIN <= gain <= MAX_GAIN:
        raise BedCheckError("the light changed too much since the empty-bed picture (x%.1f)" % gain)
    off_cb = mean(ref_cb) - mean(cur_cb)
    off_cr = mean(ref_cr) - mean(cur_cr)
    changed = 0
    for i in range(n):
        dy = abs(ref_y[i] - cur_y[i] * gain)
        dc = max(abs(ref_cb[i] - cur_cb[i] - off_cb), abs(ref_cr[i] - cur_cr[i] - off_cr)) * COLOR_WEIGHT
        if max(dy, dc) > tolerance:
            changed += 1
    score = 100.0 * changed / n
    return {"clear": score <= threshold, "score": round(score, 2), "threshold": threshold,
            "cells": n, "changed": changed, "gain": round(gain, 2), "grid": [rw, rh]}


# ---------------------------------------------------------------- per printer state

class BedChecker:
    """Reference pictures (data_dir/bed_reference/<printer>.jpg) and the check state per printer."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "bed_reference")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.state = {}       # printer id -> dict (see _state)
        self._ref_cache = {}  # printer id -> (mtime, backend, grid)

    def _path(self, pid, ext="jpg"):
        if not pid or "/" in pid or "\\" in pid or pid.startswith("."):
            raise BedCheckError("bad printer id")
        return os.path.join(self.dir, "%s.%s" % (pid, ext))

    def reference(self, pid):
        """JPEG bytes of the saved empty-bed picture, or None."""
        try:
            with open(self._path(pid), "rb") as f:
                return f.read()
        except OSError:
            return None

    def reference_info(self, pid):
        try:
            with open(self._path(pid, "json"), encoding="utf-8") as f:
                info = json.load(f)
            return info if os.path.isfile(self._path(pid)) else None
        except (OSError, ValueError):
            return {"saved": None} if os.path.isfile(self._path(pid)) else None

    def set_reference(self, pid, jpeg, user):
        grid = gray_grid(jpeg)               # refuse pictures that cannot be checked
        mean = sum(grid[2]) / max(1, len(grid[2]))  # brightness channel
        if mean < MIN_BRIGHTNESS:
            raise BedCheckError("the camera picture is too dark (is the light off?)")
        tmp = self._path(pid) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(jpeg)
        os.replace(tmp, self._path(pid))
        info = {"saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": user, "blocks": list(grid[:2]),
                "bytes": len(jpeg), "decoder": backend()}
        with open(self._path(pid, "json") + ".tmp", "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(self._path(pid, "json") + ".tmp", self._path(pid, "json"))
        with self.lock:
            self._ref_cache.pop(pid, None)
            self.state.pop(pid, None)
        return info

    def clear_reference(self, pid):
        for ext in ("jpg", "json"):
            try:
                os.remove(self._path(pid, ext))
            except OSError:
                pass
        with self.lock:
            self._ref_cache.pop(pid, None)
            self.state.pop(pid, None)

    def _reference_grid(self, pid):
        path = self._path(pid)
        mtime = os.path.getmtime(path)
        kind = backend()
        cached = self._ref_cache.get(pid)
        if cached and cached[0] == mtime and cached[1] == kind:
            return cached[2]
        grid = gray_grid(self.reference(pid) or b"")
        self._ref_cache[pid] = (mtime, kind, grid)
        return grid

    def check(self, pid, jpeg, cfg):
        """One comparison of a picture with the reference (no state change). Raises BedCheckError."""
        if not os.path.isfile(self._path(pid)):
            raise BedCheckError("no empty-bed picture saved for this printer")
        ref = self._reference_grid(pid)
        cur = gray_grid(jpeg)
        return compare(ref, cur, cfg["bed_check_roi"], cfg["bed_check_tolerance"],
                       cfg["bed_check_threshold"])

    def _state(self, pid):
        return self.state.setdefault(pid, {
            "passes": 0, "seen_occupied": False, "last_hash": None, "last_frame_time": 0.0,
            "result": None, "reason": "", "checked": None, "notified": False})

    def reset(self, pid):
        """A print started (or the job was started): forget passes and what was seen."""
        with self.lock:
            st = self._state(pid)
            st.update(passes=0, seen_occupied=False, notified=False, result=None, reason="",
                      last_hash=None)

    def observe(self, pid, jpeg, frame_time, cfg):
        """Check one camera picture and update the run of clear pictures.

        Returns (state copy, ready): ready is True only when enough fresh, different, clear
        pictures were seen in a row (and, when required, a part was seen since the last print).
        """
        with self.lock:
            st = self._state(pid)
            st["checked"] = int(time.time())
            digest = hashlib.sha1(jpeg).hexdigest() if jpeg else None
            try:
                if jpeg is None:
                    raise BedCheckError("the camera has no fresh picture")
                if frame_time <= st["last_frame_time"] or digest == st["last_hash"]:
                    raise BedCheckError("the camera picture is not changing (frozen camera?)")
                st["last_frame_time"], st["last_hash"] = frame_time, digest
                result = self.check(pid, jpeg, cfg)
            except Exception as e:  # any problem: not clear, start over
                st.update(passes=0, result=None,
                          reason=str(e) if isinstance(e, BedCheckError) else "bed check failed: %s" % e)
                if not isinstance(e, BedCheckError):
                    log.exception("bed check of %s failed", pid)
                return dict(st), False
            st["result"] = result
            if not result["clear"]:
                st.update(passes=0, seen_occupied=True,
                          reason="bed not clear (%.1f%% changed, limit %.1f%%)" % (result["score"], result["threshold"]))
                return dict(st), False
            st["passes"] += 1
            need = cfg["bed_check_frames"]
            if cfg["bed_check_require_removal"] and not st["seen_occupied"]:
                st["reason"] = ("bed looks clear, but no finished part was seen on it since the last print: "
                                "start by hand (or turn off 'require removal')")
                return dict(st), False
            if st["passes"] < need:
                st["reason"] = "bed looks clear (%d of %d pictures)" % (st["passes"], need)
                return dict(st), False
            st["reason"] = "bed clear (%.1f%% changed, limit %.1f%%)" % (result["score"], result["threshold"])
            return dict(st), True

    def take_notice(self, pid):
        """True once per wait: the first time the bed is found not clear after a print."""
        with self.lock:
            st = self._state(pid)
            if st["notified"]:
                return False
            st["notified"] = True
            return True

    def status(self, pid):
        with self.lock:
            st = dict(self._state(pid))
        st.pop("last_hash", None)
        st.pop("last_frame_time", None)
        return st
