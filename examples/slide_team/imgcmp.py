"""Compare our render with the original picture, by program instead of by a model's eye.

Two numbers per comparison, computed on pixels sampled every STEP pixels outside the mask:
- psnr: peak signal-to-noise ratio of the grey levels (higher is closer; identical pictures give inf). It punishes a
  shape that is right but a few pixels off as much as a missing shape, so it is reported, not used to decide.
- match: the picture is cut into CELL x CELL squares and each square gets the colour class that covers most of it
  (orange, green, blue, dark text, grey lines, light fills, red, peach; background when nothing covers 12%).
  match = squares where each picture's class also appears within one square in the other / squares that are not
  background in either picture. It tolerates small shifts and says WHERE the colours differ, which becomes feedback.
Reads 8-bit RGB/RGBA PNGs with the standard library only (Python 3.6+).
"""
import math
import struct
import zlib

CELL, STEP = 16, 2
CLASSES = ("orange", "peach", "green", "blue", "red", "dark", "gray", "light")


def read_png(path):
    """(width, height, rows): rows[y] is (R, G, B) as three bytes objects."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    pos, idat, header = 8, [], None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + n
    w, h, depth, ctype, _, _, interlace = header
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError(f"{path}: only 8-bit RGB or RGBA PNGs without interlacing are supported")
    bpp = 3 if ctype == 2 else 4
    raw, stride = zlib.decompress(b"".join(idat)), w * bpp
    rows, prev = [], bytearray(stride)
    for y in range(h):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError(f"{path}: unknown PNG filter {kind}")
        rows.append((bytes(line[0::bpp]), bytes(line[1::bpp]), bytes(line[2::bpp])))
        prev = line
    return w, h, rows


def colour(r, g, b):
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 95:
        return "dark"
    if r > 170 and g < 120 and b < 120 and r - g > 70:
        return "red"
    if r > 180 and 100 < g < 205 and b < 110 and r - b > 120:
        return "orange"
    if r > 235 and 205 < g < 245 and 150 < b < 230 and r - b > 25:
        return "peach"
    if g > 110 and g > r + 30 and g > b + 15:
        return "green"
    if b > 140 and b > r + 70:
        return "blue"
    if hi > 240 and hi - lo < 10:
        return None  # background: white, the slide colour, the faint grid (measured on the original: 99% of it)
    if hi > 205 and hi - lo < 40:
        return "light"
    if hi - lo < 45:
        return "gray"
    return None


def cells(image, mask=None):
    """Grid of colour classes: grid[cy][cx] is a class name, None (background) or "mask"."""
    w, h, rows = image
    gw, gh = (w + CELL - 1) // CELL, (h + CELL - 1) // CELL
    grid = []
    for cy in range(gh):
        line = []
        for cx in range(gw):
            x0, y0 = cx * CELL, cy * CELL
            if mask and mask(x0 + CELL / 2, y0 + CELL / 2):
                line.append("mask")
                continue
            counts, total = {}, 0
            for y in range(y0, min(y0 + CELL, h), STEP):
                R, G, B = rows[y]
                for x in range(x0, min(x0 + CELL, w), STEP):
                    k = colour(R[x], G[x], B[x])
                    total += 1
                    if k:
                        counts[k] = counts.get(k, 0) + 1
            best = max(counts, key=counts.get) if counts else None
            line.append(best if best and counts[best] >= 0.12 * total else None)
        grid.append(line)
    return grid


def psnr(a, b, mask=None):
    (w, h, ra), (w2, h2, rb) = a, b
    if (w, h) != (w2, h2):
        raise ValueError(f"the pictures differ in size: {w}x{h} and {w2}x{h2}")
    err, n = 0, 0
    for y in range(0, h, STEP):
        (R1, G1, B1), (R2, G2, B2) = ra[y], rb[y]
        for x in range(0, w, STEP):
            if mask and mask(x, y):
                continue
            d = (R1[x] * 299 + G1[x] * 587 + B1[x] * 114 - R2[x] * 299 - G2[x] * 587 - B2[x] * 114) / 1000
            err += d * d
            n += 1
    mse = err / max(n, 1)
    return math.inf if mse == 0 else 10 * math.log10(255 * 255 / mse)


def near(grid, cx, cy, k):
    for y in range(max(cy - 1, 0), min(cy + 2, len(grid))):
        for x in range(max(cx - 1, 0), min(cx + 2, len(grid[0]))):
            if grid[y][x] == k:
                return True
    return False


def match_cells(ga, gb, box=None):
    """Share of the non-background squares (in either picture) whose class also appears within one square in the other."""
    x0, y0, x1, y1 = box or (0, 0, len(ga[0]) * CELL, len(ga) * CELL)
    hit = considered = 0
    for cy in range(int(y0 // CELL), min(int(math.ceil(y1 / CELL)), len(ga))):
        for cx in range(int(x0 // CELL), min(int(math.ceil(x1 / CELL)), len(ga[0]))):
            ka, kb = ga[cy][cx], gb[cy][cx]
            if "mask" in (ka, kb) or (ka is None and kb is None):
                continue
            considered += 1
            # each side's colour must appear within one square on the other side (background asks nothing)
            hit += (ka is None or near(gb, cx, cy, ka)) and (kb is None or near(ga, cx, cy, kb))
    return hit / max(considered, 1)


def compare(ours, original, mask=None, regions=(), box=None, original_cells=None):
    """Scores plus per-region colour counts. regions: [(name, x0, y0, x1, y1)] in pixels; box limits the match score to
    one area (a row). original_cells: cells(original, mask) computed once by the caller, to save time."""
    ga, gb = cells(ours, mask), original_cells or cells(original, mask)
    per_region = []
    for name, x0, y0, x1, y1 in regions:
        counts = {"ours": {}, "original": {}}
        for side, grid in (("ours", ga), ("original", gb)):
            for cy in range(int(y0 // CELL), int(min(y1, len(grid) * CELL) // CELL)):
                for cx in range(int(x0 // CELL), int(min(x1, len(grid[0]) * CELL) // CELL)):
                    k = grid[cy][cx]
                    if k not in (None, "mask"):
                        counts[side][k] = counts[side].get(k, 0) + 1
        per_region.append((name, counts["ours"], counts["original"]))
    return {"match": match_cells(ga, gb, box), "psnr": psnr(ours, original, mask), "regions": per_region}


def crop(image, x0, y0, x1, y1):
    w, h, rows = image
    x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))
    return x1 - x0, y1 - y0, [tuple(channel[x0:x1] for channel in rows[y]) for y in range(y0, y1)]


def write_png(path, image):
    """8-bit RGB PNG, no filtering: enough for crops handed to the art director."""
    w, h, rows = image
    raw = bytearray()
    for R, G, B in rows:
        line = bytearray(3 * w)
        line[0::3], line[1::3], line[2::3] = R, G, B
        raw += b"\x00" + line
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


WORDS = {"orange": "orange (prefill) parts", "peach": "light orange box fill", "green": "green (KV) parts",
         "blue": "blue (decode, streams) parts", "red": "red marks", "dark": "dark text", "gray": "grey lines or borders",
         "light": "light box fills"}


def feedback(result, limit=5, least=6):
    """The biggest colour differences by region, as sentences a drawer can act on."""
    gaps = []
    for name, ours, original in result["regions"]:
        for k in CLASSES:
            a, b = ours.get(k, 0), original.get(k, 0)
            if abs(a - b) >= least:
                gaps.append((abs(a - b) / max(a, b), abs(a - b), name, k, a, b))
    gaps.sort(reverse=True)
    out = []
    for _, _, name, k, a, b in gaps[:limit]:
        if a < b:
            out.append(f"{name}: the original has more {WORDS[k]} ({b} squares, ours {a}): add or enlarge them")
        else:
            out.append(f"{name}: we have more {WORDS[k]} than the original ({a} squares, original {b}): remove, shrink or move them")
    return out
