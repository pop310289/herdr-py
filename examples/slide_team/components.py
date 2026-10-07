"""Slide components for the layout team: a small drawing library the agents call by describing components in JSON.

An agent answers with a JSON list such as [{"id": "r1.prompt", "type": "tokens", "x": 40, "y": 250, "labels": ["P", "P"]}].
check() says what is wrong in words a model can act on ("kind 'token' is not allowed; use one of ..."), and svg() draws
the slide (dashed connectors, arrowheads and stacked boxes included, which open-slide-py elements cannot express).
The svg type takes a drawer's own SVG markup for shapes the other types cannot draw; svg_markup() checks it (no scripts,
event attributes, outside links or style imports) and draw_svg() writes back only what was checked.
Standard library only; Python 3.6+.
"""
import html
import json
import math
import re
import xml.etree.ElementTree as ET

W, H = 1920, 1080
BG = "#F4F7FB"
INK = {"text": "#1B2430", "muted": "#5D6673", "red": "#D0505C", "white": "#FFFFFF", "prefill": "#C77C02", "kv": "#2E7D32",
       "decode": "#1B5FB8"}
TOKENS = {  # fill, border, letter
    "prompt": ("#E9ECEF", "#9AA4AE", "#1B2430"),
    "prefill": ("#F5A623", "#C77C02", "#1B2430"),
    "kv": ("#4CAF50", "#2E7D32", "#FFFFFF"),
    "decode": ("#2F80ED", "#1B5FB8", "#FFFFFF"),
    "empty": ("#E9ECEF", "#C5CCD3", "#5D6673"),
    "outline": ("#FFFFFF", "#2F80ED", "#1B5FB8"),
}
BOXES = {  # fill, border, title
    "gpu": ("#EAF1F8", "#6B7A8C", "#1B2430"),
    "pool": ("#F8FAFC", "#6B7A8C", "#1B2430"),
    "decode": ("#DCEBFC", "#2F80ED", "#1B5FB8"),
    "chunk": ("#FDEBD0", "#F5A623", "#9A5B00"),
    "prefill": ("#FDEBD0", "#C77C02", "#9A5B00"),
}
LINES = {"line": "#9AA4AE", "stream": "#2F80ED", "chunk": "#F5A623", "kv": "#4CAF50", "red": "#D0505C"}
NUM = "number"
# type -> {key: (kind of value, default)}; the default REQUIRED means the key must be given, None means the setting is
# optional and left out of the normalized component when not given (its drawing then follows another setting).
REQUIRED = object()
SCHEMA = {
    "text": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "text": (str, REQUIRED), "size": (NUM, 24), "color": (str, "text"),
             "bold": (bool, False), "anchor": (str, "start"), "italic": (bool, False)},
    "tokens": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "labels": (list, REQUIRED), "kind": (str, "prompt"), "w": (NUM, 44),
               "h": (NUM, 40), "gap": (NUM, 6), "border_width": (NUM, 2), "outline": (bool, False)},
    "grid": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "rows": (int, 3), "cols": (int, 3), "kind": (str, "kv"), "cell": (NUM, 28),
             "gap": (NUM, 5), "row_labels": (list, []), "cell_w": (NUM, None), "cell_h": (NUM, None)},
    "box": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "w": (NUM, REQUIRED), "h": (NUM, REQUIRED), "kind": (str, "gpu"),
            "title": (str, ""), "title_size": (NUM, 26), "stack": (int, 0), "pins": (bool, False), "border_width": (NUM, 3),
            "title_color": (str, None), "dashed": (bool, False)},
    "arrow": {"points": (list, REQUIRED), "color": (str, "line"), "dashed": (bool, False), "head": (bool, True), "width": (NUM, 3),
              "nodes": (bool, False), "curved": (bool, False), "head_size": (NUM, None)},
    "lane": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "w": (NUM, REQUIRED), "label": (str, REQUIRED), "tokens": (list, []),
             "color": (str, "decode"), "label_size": (NUM, 30)},
    "svg": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "w": (NUM, REQUIRED), "h": (NUM, REQUIRED), "markup": (str, REQUIRED)},
    "rect": {"x": (NUM, REQUIRED), "y": (NUM, REQUIRED), "w": (NUM, REQUIRED), "h": (NUM, REQUIRED), "fill": (str, "#D5DBE3")},
}
AGENT_TYPES = ("text", "tokens", "grid", "box", "arrow", "lane", "svg")  # "rect" is only for the fixed header and separators
ENUMS = {("text", "color"): INK, ("text", "anchor"): ("start", "middle", "end"), ("tokens", "kind"): TOKENS,
         ("grid", "kind"): TOKENS, ("box", "kind"): BOXES, ("box", "title_color"): INK, ("arrow", "color"): LINES,
         ("lane", "color"): INK}
CORNER = 15  # radius of a curved arrow's corners (at most half of the shorter segment next to the corner)
GUIDE = """Component types (pixels on a {w} x {h} canvas; x, y is the top-left corner):
- text:   {"id": "r1.title", "type": "text", "x": 40, "y": 160, "text": "Shared worker", "size": 32, "bold": true, "color": "text"}
- tokens: {"id": "r1.prompt", "type": "tokens", "x": 40, "y": 250, "labels": ["P", "P", "P"], "kind": "prompt"}   (each token 44 x 40, 6 apart)
- grid:   {"id": "r1.kv", "type": "grid", "x": 980, "y": 240, "rows": 3, "cols": 3, "kind": "kv", "row_labels": ["P", "A", "B"]}
- box:    {"id": "r1.gpu", "type": "box", "x": 640, "y": 190, "w": 480, "h": 260, "kind": "gpu", "title": "1 GPU", "stack": 0}
- arrow:  {"id": "r1.link", "type": "arrow", "points": [[320, 270], [640, 270]], "color": "line", "dashed": false, "head": true}
- lane:   {"id": "r1.laneA", "type": "lane", "x": 1300, "y": 330, "w": 560, "label": "A", "tokens": [{"at": 1340, "text": "A", "kind": "decode"}]}
- svg:    {"id": "r1.brace", "type": "svg", "x": 50, "y": 312, "w": 70, "h": 8, "markup": "<svg viewBox='0 0 70 8'><path d='M1 0 V7 H69 V0' fill='none' stroke='#C77C02' stroke-width='2'/></svg>"}
token and grid kinds: prompt, prefill, kv, decode, empty, outline.  box kinds: gpu, pool, decode, chunk, prefill.
arrow colors: line, stream, chunk, kv, red.  text colors: text, muted, red, white, prefill, kv, decode.
"stack": 2 draws two boxes behind (a pool of machines).  "dashed": true for live streams.  "nodes": true puts small squares on the bends.
Optional settings (leave them out for the usual look):
- text "italic": true.  tokens "outline": true (border in the kind's colour, no fill), "border_width": 1.5.
- grid "cell_w": 22, "cell_h": 31 (cells taller than wide; "cell" alone draws squares).
- box "border_width": 2, "title_color": "prefill" (a text color), "dashed": true.
- arrow "curved": true (round corners), "head_size": 10 (arrowhead length in pixels).
svg draws your own SVG (single quotes inside "markup") scaled from its viewBox into x, y, w, h: only for shapes the types
above cannot draw.  No scripts, event attributes (onclick), animation, or links and url() other than #id or a data:image."""


def guide(size=(W, H)):
    return GUIDE.replace("{w}", str(size[0])).replace("{h}", str(size[1]))


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check(components, prefix=None, size=(W, H)):
    """Return (errors, warnings, normalized). Errors name the component and say how to fix it; normalized has defaults
    filled (an optional setting whose default is None stays out until it is given)."""
    if not isinstance(components, list):
        return ["the answer must be a JSON list of components, like [{\"id\": ..., \"type\": ...}, ...]"], [], []
    errors, warnings, out, seen = [], [], [], set()
    for n, item in enumerate(components):
        where = f"item {n + 1}"
        if not isinstance(item, dict):
            errors.append(f"{where}: each component must be a JSON object with \"id\" and \"type\"")
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{where}: give it a text \"id\", like \"{prefix or 'r1.'}name\"")
            continue
        where = cid
        if cid in seen:
            errors.append(f"{where}: the id is used twice; every id must be different")
            continue
        seen.add(cid)
        if prefix and not cid.startswith(prefix):
            errors.append(f"{where}: ids in this row must start with \"{prefix}\"")
            continue
        kind_of = item.get("type")
        allowed = AGENT_TYPES if prefix else tuple(SCHEMA)
        if kind_of not in allowed:
            errors.append(f"{where}: unknown type {json.dumps(kind_of)}; use one of {', '.join(allowed)}")
            continue
        comp, bad = {"id": cid, "type": kind_of}, False
        for key, (want, default) in SCHEMA[kind_of].items():
            if key not in item:
                if default is REQUIRED:
                    errors.append(f"{where}: missing \"{key}\"")
                    bad = True
                elif default is not None:
                    comp[key] = list(default) if isinstance(default, list) else default
                continue
            value = item[key]
            ok = is_num(value) if want is NUM else (isinstance(value, int) and not isinstance(value, bool) if want is int
                                                    else isinstance(value, want))
            if not ok:
                errors.append(f"{where}: \"{key}\" must be {'a number' if want is NUM else 'a whole number' if want is int else want.__name__}")
                bad = True
                continue
            if (kind_of, key) in ENUMS and value not in ENUMS[(kind_of, key)]:
                errors.append(f"{where}: {key} {json.dumps(value)} is not allowed; use one of {', '.join(ENUMS[(kind_of, key)])}")
                bad = True
                continue
            comp[key] = value
        for key in item:
            if key not in SCHEMA[kind_of] and key not in ("id", "type"):
                warnings.append(f"{where}: \"{key}\" is not a {kind_of} setting and was ignored")
        if bad:
            continue
        problem = shape_problem(comp, size)
        if problem:
            errors.append(f"{where}: {problem}")
            continue
        out.append(comp)
    if not errors:
        warnings += overlaps(out)
    return errors, warnings, out


def shape_problem(c, size=(W, H)):
    W, H = size
    t = c["type"]
    if t in ("text", "tokens", "grid", "box", "lane", "rect", "svg") and not (0 <= c["x"] <= W and 0 <= c["y"] <= H):
        return f"x {c['x']:g}, y {c['y']:g} is outside the {W} x {H} canvas"
    if t in ("tokens", "box") and not 0 <= c["border_width"] <= 10:
        return "border_width must be 0 to 10 (pixels)"
    for key in ("cell_w", "cell_h"):
        if t == "grid" and key in c and not 4 <= c[key] <= 120:
            return f"{key} must be 4 to 120 (pixels)"
    if t == "arrow" and "head_size" in c and not 4 <= c["head_size"] <= 40:
        return "head_size must be 4 to 40 (pixels)"
    if t == "text":
        if not c["text"].strip():
            return "the text is empty"
        if not 8 <= c["size"] <= 80:
            return "size must be between 8 and 80"
    if t == "tokens":
        if not c["labels"] or len(c["labels"]) > 12 or not all(isinstance(s, str) and len(s) <= 3 for s in c["labels"]):
            return "labels must be a list of 1 to 12 short texts (at most 3 letters each), like [\"P\", \"P\"]"
    if t == "grid":
        if not (1 <= c["rows"] <= 6 and 1 <= c["cols"] <= 8):
            return "rows must be 1 to 6 and cols 1 to 8"
        if not all(isinstance(s, str) and len(s) <= 3 for s in c["row_labels"]):
            return "row_labels must be short texts, like [\"P\", \"A\", \"B\"]"
    if t in ("box", "rect", "lane", "svg"):
        width, height = c["w"], c.get("h", 1)
        if width <= 0 or height <= 0:
            return "w and h must be more than 0"
        if c["x"] + width > W + 1 or c["y"] + height > H + 1:
            return "it goes past the right or bottom edge of the canvas"
    if t == "box" and not 0 <= c["stack"] <= 3:
        return "stack must be 0 to 3"
    if t == "svg":
        return svg_markup(c["markup"])[0]
    if t == "arrow":
        pts = c["points"]
        if len(pts) < 2 or not all(isinstance(p, list) and len(p) == 2 and is_num(p[0]) and is_num(p[1]) for p in pts):
            return "points must be a list of at least two [x, y] pairs, like [[320, 270], [640, 270]]"
        if not all(0 <= p[0] <= W and 0 <= p[1] <= H for p in pts):
            return "a point is outside the canvas"
    if t == "lane":
        for tok in c["tokens"]:
            if not (isinstance(tok, dict) and is_num(tok.get("at")) and isinstance(tok.get("text", ""), str)
                    and tok.get("kind", "decode") in TOKENS):
                return "each lane token must look like {\"at\": 1340, \"text\": \"A\", \"kind\": \"decode\"}"
            if not c["x"] <= tok["at"] <= c["x"] + c["w"]:
                return f"a token at {tok['at']:g} is not on the lane (x {c['x']:g} to {c['x'] + c['w']:g})"
    return None


def text_width(s, size, bold):
    return sum((0.3 if ch in " il.,'|:;!" else 0.62 if ch.isupper() else 0.55) for ch in s) * size * (1.06 if bold else 1)


def bbox(c):
    t = c["type"]
    if t == "text":
        w = text_width(c["text"], c["size"], c["bold"])
        x0 = c["x"] - (w / 2 if c["anchor"] == "middle" else w if c["anchor"] == "end" else 0)
        return x0, c["y"], x0 + w, c["y"] + c["size"] * 1.2
    if t == "tokens":
        n = len(c["labels"])
        return c["x"], c["y"], c["x"] + n * (c["w"] + c["gap"]) - c["gap"], c["y"] + c["h"]
    if t == "grid":
        cw, ch = c.get("cell_w", c["cell"]), c.get("cell_h", c["cell"])
        return c["x"], c["y"], c["x"] + c["cols"] * (cw + c["gap"]), c["y"] + c["rows"] * (ch + c["gap"])
    if t == "svg":
        return c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]
    if t in ("box", "rect"):
        s = 7 * c.get("stack", 0)
        return c["x"], c["y"] - s, c["x"] + c["w"] + s, c["y"] + c["h"]
    if t == "arrow":
        xs, ys = [p[0] for p in c["points"]], [p[1] for p in c["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    if t == "lane":
        label = text_width(c["label"], c["label_size"], True) + 14
        return c["x"] - label, c["y"] - 22, c["x"] + c["w"], c["y"] + 22
    raise ValueError(t)


def overlaps(comps):
    texts = [c for c in comps if c["type"] == "text"]
    found = []
    for i, a in enumerate(texts):
        ax0, ay0, ax1, ay1 = bbox(a)
        for b in texts[i + 1:]:
            bx0, by0, bx1, by1 = bbox(b)
            if min(ax1, bx1) - max(ax0, bx0) > 2 and min(ay1, by1) - max(ay0, by0) > 2:
                found.append(f"texts {a['id']} and {b['id']} overlap; move one of them")
    return found


def labels(comps):
    """Every piece of text a reader sees, for the required-label check."""
    out = []
    for c in comps:
        if c["type"] == "text":
            out.append(c["text"])
        elif c["type"] in ("box",) and c["title"]:
            out.append(c["title"])
        elif c["type"] == "lane":
            out.append(c["label"])
            out += [t.get("text", "") for t in c["tokens"]]
        elif c["type"] == "tokens":
            out += c["labels"]
        elif c["type"] == "grid":
            out += c["row_labels"]
        elif c["type"] == "svg":
            root = svg_markup(c["markup"])[1]
            out += ["".join(e.itertext()) for e in (root.iter() if root is not None else ()) if local(e.tag).lower() == "text"]
    return out


def missing(comps, required):
    seen = " | ".join(labels(comps)).lower()
    return [r for r in required if r.lower() not in seen]


def esc(s):
    return html.escape(str(s), quote=True)


def svg(comps, size=(W, H), view=None, scale=1):
    """Draw the components; view = (x, y, w, h) crops the drawing (one row) without changing coordinates."""
    vx, vy, vw, vh = view or (0, 0) + tuple(size)
    order = {"rect": 0, "box": 1, "arrow": 2, "lane": 2, "grid": 3, "tokens": 3, "svg": 3, "text": 4}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw * scale:g}" height="{vh * scale:g}" viewBox="{vx} {vy} {vw} {vh}" '
           f'font-family="Arial, Helvetica, sans-serif">', "<defs>"]
    for name, color in LINES.items():
        out.append(f'<marker id="head-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" '
                   f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
    sized = []  # arrowheads with a head_size: one marker per colour and size, measured in pixels instead of line widths
    for c in comps:
        if c["type"] == "arrow" and c["head"] and "head_size" in c and (c["color"], c["head_size"]) not in sized:
            sized.append((c["color"], c["head_size"]))
    for name, length in sized:
        out.append(f'<marker id="{head_id(name, length)}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" '
                   f'markerWidth="{length:g}" markerHeight="{length:g}" orient="auto-start-reverse">'
                   f'<path d="M0,0 L10,5 L0,10 z" fill="{LINES[name]}"/></marker>')
    out.append(f'</defs><rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="{BG}"/>')
    for c in sorted(comps, key=lambda c: order[c["type"]]):
        out.append(globals()["draw_" + c["type"]](c))
    out.append("</svg>")
    return "\n".join(out)


def head_id(color, length=None):
    """Marker id of an arrowhead: head-<color> (5 line widths long) or head-<color>-<length in pixels>."""
    return f"head-{color}" if length is None else f"head-{color}-{length:g}".replace(".", "_")


def draw_rect(c):
    return f'<rect x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" fill="{esc(c["fill"])}"/>'


def draw_text(c):
    weight = ' font-weight="bold"' if c["bold"] else ""
    style = ' font-style="italic"' if c.get("italic") else ""
    return (f'<text x="{c["x"]}" y="{c["y"] + c["size"] * 0.86:.1f}" font-size="{c["size"]}" fill="{INK[c["color"]]}"'
            f' text-anchor="{c["anchor"]}"{weight}{style}>{esc(c["text"])}</text>')


def token(x, y, w, h, label, kind, border_width=2, outline=False):
    fill, border, ink = TOKENS[kind]
    if outline:  # the kind's border colour and no fill; a white letter would vanish, so it takes the border colour
        fill, ink = "none", (border if ink == "#FFFFFF" else ink)
    out = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{border}" stroke-width="{border_width:g}"/>'
    if label:
        out += (f'<text x="{x + w / 2:.1f}" y="{y + h * 0.68:.1f}" font-size="{h * 0.5:.0f}" font-weight="bold" fill="{ink}" '
                f'text-anchor="middle">{esc(label)}</text>')
    return out


def draw_tokens(c):
    return "".join(token(c["x"] + i * (c["w"] + c["gap"]), c["y"], c["w"], c["h"], s, c["kind"], c.get("border_width", 2),
                         c.get("outline", False)) for i, s in enumerate(c["labels"]))


def draw_grid(c):
    out = []
    cw, ch = c.get("cell_w", c["cell"]), c.get("cell_h", c["cell"])  # cell alone draws squares
    for r in range(c["rows"]):
        y = c["y"] + r * (ch + c["gap"])
        if r < len(c["row_labels"]) and c["row_labels"][r]:
            out.append(f'<text x="{c["x"] - 8}" y="{y + ch * 0.72:.1f}" font-size="{ch * 0.6:.0f}" fill="{INK["muted"]}" '
                       f'text-anchor="end">{esc(c["row_labels"][r])}</text>')
        for k in range(c["cols"]):
            out.append(token(c["x"] + k * (cw + c["gap"]), y, cw, ch, "", c["kind"]))
    return "".join(out)


def draw_box(c):
    fill, border, ink = BOXES[c["kind"]]
    width = c.get("border_width", 3)
    dash = f' stroke-dasharray="{width * 3:g} {width * 2:g}"' if c.get("dashed") else ""
    ink = INK[c["title_color"]] if "title_color" in c else ink
    out = []
    for k in range(c["stack"], 0, -1):  # machines behind, up and to the right, drawn with 2/3 of the border width
        out.append(f'<rect x="{c["x"] + 7 * k}" y="{c["y"] - 7 * k}" width="{c["w"]}" height="{c["h"]}" rx="12" fill="#FFFFFF" '
                   f'stroke="{border}" stroke-width="{round(width * 2 / 3, 2):g}"{dash}/>')
    out.append(f'<rect x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" rx="12" fill="{fill}" stroke="{border}" '
               f'stroke-width="{width:g}"{dash}/>')
    if c["title"]:
        out.append(f'<text x="{c["x"] + c["w"] / 2:.1f}" y="{c["y"] + c["title_size"] * 1.25:.1f}" font-size="{c["title_size"]}" '
                   f'font-weight="bold" fill="{ink}" text-anchor="middle">{esc(c["title"])}</text>')
    if c["pins"]:
        n = 7
        x0 = c["x"] + c["w"] / 2 - (n * 14 - 4) / 2
        out += [f'<rect x="{x0 + i * 14:.1f}" y="{c["y"] + c["h"] - 2}" width="10" height="9" rx="2" fill="#F5A623"/>' for i in range(n)]
    return "".join(out)


def draw_arrow(c):
    color = LINES[c["color"]]
    pts = " ".join(f"{p[0]},{p[1]}" for p in c["points"])
    dash = ' stroke-dasharray="12 9"' if c["dashed"] else ""
    head = f' marker-end="url(#{head_id(c["color"], c.get("head_size"))})"' if c["head"] else ""
    if c.get("curved"):
        out = (f'<path d="{rounded(c["points"])}" fill="none" stroke="{color}" stroke-width="{c["width"]}"{dash}{head} '
               f'stroke-linejoin="round"/>')
    else:
        out = f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{c["width"]}"{dash}{head} stroke-linejoin="round"/>'
    if c["nodes"]:
        out += "".join(f'<rect x="{p[0] - 8}" y="{p[1] - 8}" width="16" height="16" rx="4" fill="{color}" fill-opacity="0.85" '
                       f'stroke="#FFFFFF" stroke-width="2"/>' for p in c["points"][1:-1])
    return out


def rounded(points, radius=CORNER):
    """Path data through the points with every bend rounded: the line stops `radius` pixels before the corner (at most
    half of the shorter segment next to it) and a quadratic curve with the corner as control point joins the next one."""
    d = [f"M{points[0][0]:g},{points[0][1]:g}"]
    for (ax, ay), (bx, by), (cx, cy) in zip(points, points[1:], points[2:]):
        before, after = math.hypot(bx - ax, by - ay), math.hypot(cx - bx, cy - by)
        r = min(radius, before / 2, after / 2)
        if r <= 0:  # a repeated point: nothing to round
            d.append(f"L{bx:g},{by:g}")
            continue
        d.append(f"L{bx - (bx - ax) * r / before:.1f},{by - (by - ay) * r / before:.1f} "
                 f"Q{bx:g},{by:g} {bx + (cx - bx) * r / after:.1f},{by + (cy - by) * r / after:.1f}")
    d.append(f"L{points[-1][0]:g},{points[-1][1]:g}")
    return " ".join(d)


def draw_lane(c):
    ink = INK[c["color"]]
    out = [f'<text x="{c["x"] - 14}" y="{c["y"] + c["label_size"] * 0.35:.1f}" font-size="{c["label_size"]}" font-weight="bold" '
           f'fill="{ink}" text-anchor="end">{esc(c["label"])}</text>',
           f'<polyline points="{c["x"]},{c["y"]} {c["x"] + c["w"]},{c["y"]}" stroke="{LINES["line"]}" stroke-width="2" '
           f'marker-end="url(#head-line)"/>']
    for t in c["tokens"]:
        out.append(token(t["at"] - 21, c["y"] - 20, 42, 40, t.get("text", ""), t.get("kind", "decode")))
    return "".join(out)


# ---- the svg component: a drawer's own SVG markup, checked before it is drawn
# The slide is opened from a file by a browser (headless Chrome for the picture match, people for a look), so markup
# must not run code, fetch anything, read local files or change the rest of the slide. Each rule below has a test.
SVG_NS, XLINK_NS, XML_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink", "http://www.w3.org/XML/1998/namespace"
MAX_MARKUP, MAX_ELEMENTS, MAX_DEPTH = 20000, 400, 20  # characters, elements, levels of nesting
REFUSED = {"script": "a slide runs no code", "foreignobject": "draw with SVG shapes, not HTML",
           "iframe": "a slide embeds no pages or plugins", "embed": "a slide embeds no pages or plugins",
           "object": "a slide embeds no pages or plugins"}
ANIMATION = ("animate", "animatecolor", "animatemotion", "animatetransform", "discard", "mpath", "set")
SVG_ELEMENTS = frozenset((  # lower case; anything else is refused (HTML tags such as <meta> or <img> included)
    "svg", "g", "defs", "symbol", "use", "switch", "title", "desc", "metadata", "a", "style", "rect", "circle", "ellipse",
    "line", "polyline", "polygon", "path", "text", "tspan", "textpath", "image", "lineargradient", "radialgradient", "stop",
    "pattern", "clippath", "mask", "marker", "filter", "feblend", "fecolormatrix", "fecomponenttransfer", "fecomposite",
    "feconvolvematrix", "fediffuselighting", "fedisplacementmap", "fedistantlight", "fedropshadow", "feflood", "fefunca",
    "fefuncb", "fefuncg", "fefuncr", "fegaussianblur", "feimage", "femerge", "femergenode", "femorphology", "feoffset",
    "fepointlight", "fespecularlighting", "fespotlight", "fetile", "feturbulence"))
SAFE_LINK = re.compile(r"(#[A-Za-z_][\w.:-]*|data:image/(png|jpeg|gif);base64,[A-Za-z0-9+/=\s]*)\Z")
LINK_RULE = "links may only point to #id inside the markup or to a data:image/png, jpeg or gif;base64 picture"


def local(tag):
    """'{http://www.w3.org/2000/svg}rect' -> 'rect'."""
    return tag.rsplit("}", 1)[-1]


def url_problem(text):
    """Every url(...) must name an #id or carry a data:image; anything else would be fetched by the browser."""
    for found in re.finditer(r"url\s*\(", text, re.I):
        end = text.find(")", found.end())
        target = text[found.end():len(text) if end == -1 else end].strip().strip("'\"").strip()
        if end == -1 or not SAFE_LINK.match(target):
            return f"url({target[:40]}) is not allowed; use url(#id) or a data:image/png, jpeg or gif;base64 picture"
    return None


def css_problem(css):
    """A style attribute or <style> text: no escapes (they could spell url or @import), no imports, no image loaders."""
    if "\\" in css:
        return "CSS escapes (backslashes) are not allowed in styles"
    if re.search(r"@import", css, re.I):
        return "CSS @import is not allowed; write the styles in the markup itself"
    if re.search(r"(image-set|\bimage|\bsrc)\s*\(", css, re.I):
        return "image-set(), image() and src() are not allowed in styles; use url(#id) or a data:image"
    return url_problem(css)


def element_problem(el):
    tag = local(el.tag)
    name = tag.lower()
    if name in REFUSED:
        return f"<{tag}> is not allowed: {REFUSED[name]}"
    if name in ANIMATION:
        return f"<{tag}> is not allowed: the slide is a still picture"
    if name not in SVG_ELEMENTS:
        return f"<{tag}> is not an SVG drawing element; use rect, circle, ellipse, line, polyline, polygon, path, text or g"
    for key, value in el.attrib.items():
        attr = local(key)
        if attr.lower().startswith("on"):
            return f"the event attribute {attr} is not allowed"
        if (attr.lower() in ("href", "src") or key == f"{{{XML_NS}}}base") and not SAFE_LINK.match(value.strip()):
            return f"{attr} {json.dumps(value[:40])} is not allowed; {LINK_RULE}"
        problem = css_problem(value) if attr.lower() == "style" else url_problem(value)
        if problem:
            return problem
    return css_problem("".join(el.itertext())) if name == "style" else None


def view_box(root, whole):
    """(problem, "x y w h" or None): the markup's own viewBox, else its width and height, else None (one unit = 1 px)."""
    given = root.get("viewBox")
    if given is None:
        sizes = [re.match(r"\s*(\d+(\.\d+)?)\s*(px)?\s*\Z", root.get(k, "")) for k in ("width", "height")]
        usable = whole and all(sizes) and all(float(s.group(1)) > 0 for s in sizes)
        return None, (f"0 0 {sizes[0].group(1)} {sizes[1].group(1)}" if usable else None)
    try:
        nums = [float(v) for v in re.split(r"[\s,]+", given.strip())]
    except ValueError:
        nums = []
    if len(nums) != 4 or not all(math.isfinite(v) for v in nums) or nums[2] <= 0 or nums[3] <= 0:
        return "viewBox must be four numbers like '0 0 24 24' (width and height more than 0)", None
    return None, " ".join(f"{v:g}" for v in nums)


def svg_markup(markup):
    """Check an svg component's markup: (problem, root, viewBox). problem is None when it is safe to draw; root is the
    parsed <svg> element (shapes given without one are put inside one) and viewBox its "x y w h" or None."""
    if not markup.strip():
        return "markup is empty; give SVG shapes, like <svg viewBox='0 0 10 10'><rect width='10' height='10'/></svg>", None, None
    if len(markup) > MAX_MARKUP:
        return f"markup is {len(markup)} characters; keep it under {MAX_MARKUP}", None, None
    if re.search(r"<!\s*(doctype|entity)", markup, re.I):
        return "remove the DOCTYPE or ENTITY declaration; start the markup with <svg viewBox='...'>", None, None
    if "javascript:" in re.sub(r"[\x00-\x20]+", "", html.unescape(markup)).lower():
        return "javascript: is not allowed anywhere in the markup", None, None
    whole = re.match(r"\s*<(\?xml|svg\b)", markup) is not None
    try:
        root = ET.fromstring(markup if whole else f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}">{markup}</svg>')
    except (ET.ParseError, ValueError) as exc:  # ValueError: a lone surrogate (JSON "\ud800") cannot be encoded
        return f"the markup is not well-formed XML ({exc}); close every tag and quote every value", None, None
    if local(root.tag) != "svg":
        return "the markup must be one <svg viewBox='...'> element, or shapes without an <svg> around them", None, None
    todo, elements = [(root, 1)], []
    while todo:  # sizes first, without recursion, so the checks and the writer below never go deep
        el, depth = todo.pop()
        if len(elements) == MAX_ELEMENTS:
            return f"the markup has more than {MAX_ELEMENTS} elements; draw fewer shapes", None, None
        if depth > MAX_DEPTH:
            return f"the markup nests elements more than {MAX_DEPTH} deep", None, None
        elements.append(el)
        todo += [(child, depth + 1) for child in el]
    for el in elements:
        problem = element_problem(el)
        if problem:
            return problem, None, None
    problem, view = view_box(root, whole)
    return (problem, None, None) if problem else (None, root, view)


def attributes(el):
    """The attributes to write: plain names as given, xlink:href as href (a plain href wins), xml:* kept, others dropped."""
    out = {}
    for key, value in el.attrib.items():
        if not key.startswith("{"):
            out[key] = value
        elif key == f"{{{XLINK_NS}}}href" and "href" not in el.attrib:
            out["href"] = value
        elif key.startswith(f"{{{XML_NS}}}"):
            out["xml:" + local(key)] = value
    return out


def write_element(el):
    tag = local(el.tag)
    attrs = "".join(f' {k}="{esc(v)}"' for k, v in attributes(el).items())
    return f"<{tag}{attrs}>{write_inside(el)}</{tag}>"


def write_inside(el):
    return esc(el.text or "") + "".join(write_element(child) + esc(child.tail or "") for child in el)


def draw_svg(c):
    problem, root, view = svg_markup(c["markup"])
    if problem:  # check() refuses such markup; never write anything that was not checked
        return (f'<rect x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" fill="none" stroke="{INK["red"]}" '
                f'stroke-width="2" stroke-dasharray="6 4"/>')
    view = view or f"0 0 {c['w']:g} {c['h']:g}"
    keep = "".join(f' {k}="{esc(v)}"' for k, v in attributes(root).items() if k not in ("x", "y", "width", "height", "viewBox"))
    return (f'<svg x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" viewBox="{view}"{keep}>'
            f'{write_inside(root)}</svg>')


def parse_reply(text):
    """The JSON list in a model's reply: a ```json block, or the first [...] in the text. None when there is none."""
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    for chunk in blocks + [text]:
        start, end = chunk.find("["), chunk.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(chunk[start:end + 1])
            except ValueError:
                continue
    return None
