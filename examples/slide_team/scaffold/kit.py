"""Drawing helpers for the slide team: open-slide-py elements on one 1920 x 1080 slide. Standard library only."""
import json

ELEMENTS = []
COLORS = {"text": "#1B2430", "muted": "#5D6673", "prefill": "#F5A623", "prefill_border": "#C77C02", "kv": "#4CAF50",
          "kv_border": "#2E7D32", "decode": "#2F80ED", "decode_border": "#1B5FB8", "token": "#E9ECEF", "token_border": "#9AA4AE",
          "box": "#EAF1F8", "box_border": "#6B7A8C", "line": "#9AA4AE", "sep": "#D5DBE3", "red": "#D0505C", "white": "#FFFFFF"}
_ids = set()


def _id(base):
    name, i = base, 1
    while name in _ids:
        i += 1
        name = "%s-%d" % (base, i)
    _ids.add(name)
    return name


def text(x, y, w, h, s, size=24, color=COLORS["text"], bold=False, align="left", name="text"):
    """A text box. (x, y) is the top-left corner; h should be about 1.3 x size."""
    ELEMENTS.append({"id": _id(name), "type": "text", "text": s, "x": x, "y": y, "width": w, "height": h, "font_size": size,
                     "color": color, "bold": bold, "align": align, "font_family": "Arial"})


def box(x, y, w, h, fill=COLORS["box"], stroke=COLORS["box_border"], radius=10, stroke_width=2, name="box"):
    """A rectangle with rounded corners (GPU box, pool, chunk box...)."""
    ELEMENTS.append({"id": _id(name), "type": "rect", "x": x, "y": y, "width": w, "height": h, "fill": fill, "stroke": stroke,
                     "stroke_width": stroke_width, "radius": radius})


def token(x, y, label, kind="prompt", w=44, h=40, name="token"):
    """One token box. kind: prompt (grey P), prefill (orange), kv (green), decode (blue)."""
    fill, border, ink = {"prompt": (COLORS["token"], COLORS["token_border"], COLORS["text"]),
                         "prefill": (COLORS["prefill"], COLORS["prefill_border"], COLORS["text"]),
                         "kv": (COLORS["kv"], COLORS["kv_border"], COLORS["white"]),
                         "decode": (COLORS["decode"], COLORS["decode_border"], COLORS["white"])}[kind]
    box(x, y, w, h, fill=fill, stroke=border, radius=6, name=name)
    if label:
        text(x, y + int(h * 0.12), w, int(h * 0.8), label, size=int(h * 0.5), color=ink, bold=True, align="center", name=name + "-label")


def tokens(x, y, labels, kind="prompt", gap=6, w=44, h=40, name="token"):
    """A row of token boxes, left to right: tokens(40, 250, "PPPPPP") or tokens(1300, 300, ["A", "", "A"], "decode")."""
    for i, label in enumerate(labels):
        token(x + i * (w + gap), y, label, kind, w, h, name)


def line(x, y, w, h, color=COLORS["line"], width=2, name="line"):
    """A straight line from (x, y) to (x + w, y + h); w or h may be 0."""
    ELEMENTS.append({"id": _id(name), "type": "line", "x": x, "y": y, "width": w, "height": h, "stroke": color, "stroke_width": width})


def dot(x, y, r=5, color=COLORS["line"], name="dot"):
    """A small filled circle centred on (x, y): use it as an arrow tip at the end of a line."""
    ELEMENTS.append({"id": _id(name), "type": "ellipse", "x": x - r, "y": y - r, "width": 2 * r, "height": 2 * r, "fill": color,
                     "stroke": color, "stroke_width": 1})


def save(path="deck.json"):
    deck = {"schema_version": 1, "id": "llm-serving", "title": "LLM Serving: When to Split Prefill and Decode", "width": 1920,
            "height": 1080, "lang": "en-US",
            "slides": [{"id": "s1", "title": "LLM Serving", "background": "#F4F7FB", "elements": ELEMENTS}]}
    with open(path, "w") as handle:
        json.dump(deck, handle, indent=1)
