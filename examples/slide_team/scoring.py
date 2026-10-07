"""A stricter picture score next to imgcmp's match: it also checks the colour of borders, lines and text.

Why: imgcmp's match gives each 16 px square the colour that covers most of it, so a 3 px border or the letters of a title
never decide a square. In a real run (2026-10-07) a drawer turned an orange-bordered box with an orange title into a
blue-bordered box with a blue title; its light fill now matched the original's light fill, match rose 0.562 -> 0.634 and
the wrong revision was kept. This module adds two parts that look only at edges, next to the fill match:
- stroke: borders, lines and token outlines, i.e. edge squares that do not look like lettering;
- text: squares that look like lettering (edges in both directions, many of them): dark text or coloured text;
- fill: imgcmp's match, unchanged.
How: a pixel sampled every STEP pixels is an edge when its green channel (the main part of luminance) differs by EDGE or
more from one of its four neighbours. It takes the ink colour (ink()) of the darkest pixel within two pixels across or
along, i.e. the core of the stroke, not its anti-aliased rim: a white letter on a blue token counts as blue, a brown title
on a light fill as orange. A colour counts in a square with at least LEAST of its edge samples and SHARE of them, and
each colour that counts must also count within one square in the other picture (the same tolerance as match), in both
directions. Each part is the mean, over the ink colours seen in the area (at least SUPPORT squares), of the share of
squares found in the other picture: a colour that disappears from a row (an orange box turned blue) weighs as much as all
the dark text around it. strict = (fill + stroke + text) / 3.
scoring_calibrate.py renders the reference layout and degraded copies with Chrome and prints the numbers per row.

Switching the layout team over (layout_team.py is not changed here; compare() also works in score_mode="match", which
returns today's score and today's notes, so the extra parts can be logged before the switch):
    import scoring
    self.prepared = scoring.prepare(self.original, phone_icons)                          # once, in __init__
    result = scoring.compare(ours, self.original, phone_icons, box=..., regions=..., prepared=self.prepared,
                             score_mode=self.a.score)                                  # in evaluate(), instead of imgcmp.compare
    score = score_of(result["score"], missing)                                         # result["match"] stays the old number
    notes = scoring.feedback(result, limit=4)                                          # colour notes only in strict mode
and add ap.add_argument("--score", choices=scoring.SCORE_MODES, default="match"); log result["strict"], ["stroke"] and
["text"] with match in the history so runs in either mode can be compared.
Standard library only; Python 3.6+.
"""
import math

import imgcmp

CELL, STEP = imgcmp.CELL, imgcmp.STEP
EDGE = 40          # green-channel step to a neighbour that makes a sampled pixel an edge
LEAST, SHARE = 2, 0.25   # a colour counts in a square with at least LEAST of its edge samples and SHARE of them
SUPPORT = 4        # squares a colour needs in the area (both pictures together) to get its own share in a part
TEXT_BOTH, TEXT_MIN = 5, 14   # lettering: at least TEXT_BOTH edges each way across and TEXT_MIN edges in the square
INKS = ("orange", "green", "blue", "red", "dark")
BIT = {k: 1 << n for n, k in enumerate(INKS)}
SCORE_MODES = ("match", "strict")
INK_WORDS = {"orange": "orange (prefill)", "green": "green (KV)", "blue": "blue (decode)", "red": "red",
             "dark": "dark (black or grey)"}
PART_WORDS = {("stroke",): ("borders and lines", "are"), ("text",): ("text", "is"), ("stroke", "text"): ("borders, lines and text", "are")}


def ink(r, g, b):
    """Colour family of a stroke pixel: orange (brown included), green, blue, red, dark (black, slate, grey) or None when
    the pixel is too light to be ink (paper, light fills). As in imgcmp.colour, a pixel whose brightest channel is below
    95 is dark: the original's near-black brown letters on grey prompt tokens are dark text, not orange. Blue needs more
    colour than the others because slate borders and navy text are slightly blue."""
    if 299 * r + 587 * g + 114 * b > 215000:  # luminance above 215 of 255
        return None
    hi, lo = max(r, g, b), min(r, g, b)
    chroma = hi - lo
    if hi >= 95 and chroma >= 28:
        if hi == r:
            hue = (60.0 * (g - b) / chroma) % 360
        elif hi == g:
            hue = 60.0 * (b - r) / chroma + 120
        else:
            hue = 60.0 * (r - g) / chroma + 240
        if 20 <= hue < 70:
            return "orange"
        if 70 <= hue < 170 and chroma >= 30:
            return "green"
        if 170 <= hue < 290 and chroma >= 50:
            return "blue"
        if (hue >= 290 or hue < 20) and chroma >= 40:
            return "red"
    return "dark"


def inks(image, mask=None):
    """Grid of edge squares: grid[cy][cx] is None (no ink edges, or masked by its centre like imgcmp.cells) or
    (looks_like_text, {ink colour: edge samples}). The 3 pixels along the picture's border are not sampled."""
    w, h, rows = image
    gw, gh = (w + CELL - 1) // CELL, (h + CELL - 1) // CELL
    blocked = mask and [[bool(mask(cx * CELL + CELL / 2, cy * CELL + CELL / 2)) for cx in range(gw)] for cy in range(gh)]
    acc = [[None] * gw for _ in range(gh)]
    known = {}
    for y in range(3, h - 3, STEP):
        G, up, down, up2, down2 = rows[y][1], rows[y - 1][1], rows[y + 1][1], rows[y - 2][1], rows[y + 2][1]
        line, skip = acc[y // CELL], blocked[y // CELL] if blocked else None
        for x in range(3, w - 3, STEP):
            g, left, right, above, below = G[x], G[x - 1], G[x + 1], up[x], down[x]
            across = g - left >= EDGE or left - g >= EDGE or g - right >= EDGE or right - g >= EDGE
            along = g - above >= EDGE or above - g >= EDGE or g - below >= EDGE or below - g >= EDGE
            if not (across or along) or (skip and skip[x // CELL]):
                continue
            # the ink is the darkest pixel within two pixels across or along: the core of an anti-aliased stroke
            px, py, darkest = x, y, g
            for vx, vy, v in ((x - 1, y, left), (x + 1, y, right), (x - 2, y, G[x - 2]), (x + 2, y, G[x + 2]),
                              (x, y - 1, above), (x, y + 1, below), (x, y - 2, up2[x]), (x, y + 2, down2[x])):
                if v < darkest:
                    px, py, darkest = vx, vy, v
            R, Gp, B = rows[py]
            rgb = (R[px], Gp[px], B[px])
            k = known.get(rgb, "?")
            if k == "?":
                k = known[rgb] = ink(*rgb)
            if k is None:
                continue
            square = line[x // CELL]
            if square is None:
                square = line[x // CELL] = [0, 0, {}]
            square[0] += across
            square[1] += along
            square[2][k] = square[2].get(k, 0) + 1
    return [[None if s is None else (min(s[0], s[1]) >= TEXT_BOTH and sum(s[2].values()) >= TEXT_MIN, s[2]) for s in line]
            for line in acc]


def counted(square):
    """The ink colours that count in a square: at least LEAST edge samples and SHARE of the square's samples (a few
    anti-aliased pixels at a stroke's edge are not a colour of their own)."""
    least = max(LEAST, SHARE * sum(square[1].values()))
    return [k for k, n in square[1].items() if n >= least]


def spread(grid):
    """Per square: bitmask of the ink colours that count within one square."""
    gh, gw = len(grid), len(grid[0])
    own = [[0] * gw for _ in range(gh)]
    for cy in range(gh):
        for cx in range(gw):
            if grid[cy][cx]:
                for k in counted(grid[cy][cx]):
                    own[cy][cx] |= BIT[k]
    out = [[0] * gw for _ in range(gh)]
    for cy in range(gh):
        for cx in range(gw):
            bits = 0
            for y in range(max(cy - 1, 0), min(cy + 2, gh)):
                for x in range(max(cx - 1, 0), min(cx + 2, gw)):
                    bits |= own[y][x]
            out[cy][cx] = bits
    return out


def strongest(grid, cx, cy):
    """The ink colour with the most edge samples within one square, or None."""
    counts = {}
    for y in range(max(cy - 1, 0), min(cy + 2, len(grid))):
        for x in range(max(cx - 1, 0), min(cx + 2, len(grid[0]))):
            if grid[y][x]:
                for k, n in grid[y][x][1].items():
                    counts[k] = counts.get(k, 0) + n
    return max(sorted(counts), key=counts.get) if counts else None


def ink_parts(ours, original, box=None):
    """Compare two inks() grids. Returns (parts, tally, misses): parts = {"stroke": share, "text": share}; tally[part][colour]
    = [squares found in the other picture, squares]; misses = [(cx, cy, part, original's colour or None, ours or None)]:
    a colour of the original that ours lacks there (with what ours has there instead), or a colour of ours where the
    original has no ink at all."""
    near_ours, near_original = spread(ours), spread(original)
    gh, gw = len(ours), len(ours[0])
    x0, y0, x1, y1 = box or (0, 0, gw * CELL, gh * CELL)
    tally, misses = {"stroke": {}, "text": {}}, []
    for cy in range(int(y0 // CELL), min(int(math.ceil(y1 / CELL)), gh)):
        for cx in range(int(x0 // CELL), min(int(math.ceil(x1 / CELL)), gw)):
            for theirs, grid, other in ((False, ours, near_original), (True, original, near_ours)):
                square = grid[cy][cx]
                if not square:
                    continue
                part = "text" if square[0] else "stroke"
                for k in counted(square):
                    t = tally[part].setdefault(k, [0, 0])
                    t[1] += 1
                    if other[cy][cx] & BIT[k]:
                        t[0] += 1
                    elif theirs:
                        misses.append((cx, cy, part, k, strongest(ours, cx, cy)))
                    elif not near_original[cy][cx]:
                        misses.append((cx, cy, part, None, k))
    parts = {}
    for part, colours in tally.items():
        shares = [found / n for found, n in colours.values() if n >= SUPPORT] or \
                 [sum(f for f, _ in colours.values()) / max(sum(n for _, n in colours.values()), 1)]
        parts[part] = sum(shares) / len(shares)
    return parts, tally, misses


def prepare(original, mask=None):
    """What compare() needs from the original; compute it once per run (same mask as compare)."""
    return {"cells": imgcmp.cells(original, mask), "inks": inks(original, mask)}


def compare(ours, original, mask=None, regions=(), box=None, prepared=None, score_mode="strict"):
    """imgcmp.compare's result (match, psnr, regions) plus fill, stroke, text, strict, and score: strict in score_mode
    "strict", match in score_mode "match" (today's rule). All parts are computed in both modes so they can be logged."""
    if score_mode not in SCORE_MODES:
        raise ValueError(f"score_mode must be one of {', '.join(SCORE_MODES)}, not {score_mode!r}")
    if ours[:2] != original[:2]:
        raise ValueError(f"the pictures differ in size: {ours[0]}x{ours[1]} and {original[0]}x{original[1]}")
    prepared = prepared or prepare(original, mask)
    result = imgcmp.compare(ours, original, mask, regions, box, original_cells=prepared["cells"])
    parts, tally, misses = ink_parts(inks(ours, mask), prepared["inks"], box)
    result.update(fill=result["match"], stroke=parts["stroke"], text=parts["text"], tally=tally, score_mode=score_mode)
    result["strict"] = (result["fill"] + result["stroke"] + result["text"]) / 3
    result["score"] = result["strict"] if score_mode == "strict" else result["match"]
    result["ink_misses"] = [(name, [m for m in misses if x0 <= (m[0] + 0.5) * CELL < x1 and y0 <= (m[1] + 0.5) * CELL < y1])
                            for name, x0, y0, x1, y1 in regions]
    return result


def ink_feedback(result, limit=3, least=4):
    """Colour differences of borders, lines and text by region as sentences a drawer can act on, the ones that cost the
    score most first: a miss costs its colour's share 1 / (that colour's squares in the part), so a lost orange border
    comes before as many squares of dark text."""
    groups = {}
    for name, misses in result.get("ink_misses", ()):
        for _, _, part, theirs, ours in misses:
            g = groups.setdefault((name, theirs, ours), {})
            g[part] = g.get(part, 0) + 1
    tally = result.get("tally", {})

    def cost(colour, parts):
        return sum(n / max(tally.get(part, {}).get(colour, (0, 0))[1], 1) for part, n in parts.items())
    ranked = sorted(((cost(theirs or ours, p), sum(p.values()), name, theirs, ours, p)
                     for (name, theirs, ours), p in groups.items()), key=lambda g: (-g[0], -g[1], g[2], str(g[3]), str(g[4])))
    out = []
    for _, n, name, theirs, ours, parts in ranked:
        if n < least:
            continue
        what, verb = PART_WORDS[tuple(p for p in ("stroke", "text") if p in parts)]
        if theirs and ours:
            out.append(f"{name}: the original's {what} {verb} {INK_WORDS[theirs]}, ours {verb} {INK_WORDS[ours]} ({n} squares): "
                       f"change them to {INK_WORDS[theirs]}")
        elif theirs:
            out.append(f"{name}: the original has {INK_WORDS[theirs]} {what} here that ours lacks ({n} squares): "
                       "add them or move ours there")
        else:
            out.append(f"{name}: ours has {INK_WORDS[ours]} {what} where the original has none ({n} squares): "
                       "remove or move them")
        if len(out) == limit:
            break
    return out


def feedback(result, limit=5, least=6):
    """Notes for the drawers. score_mode "match": exactly imgcmp.feedback. "strict": colour notes for borders, lines and
    text first (at most half of the limit, at least one), then the fill notes, then more colour notes if room is left."""
    fill = imgcmp.feedback(result, limit=limit, least=least)
    if result.get("score_mode") != "strict":
        return fill
    colour = ink_feedback(result, limit=limit)
    first = colour[:max(1, limit // 2)]
    out = first + fill[:limit - len(first)]
    return out + colour[len(first):len(first) + limit - len(out)]
