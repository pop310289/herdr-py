"""A stand-in for headless Chrome in tests: draws the filled rectangles of the SVG straight into a PNG (imgcmp.write_png).

Text, lines and borders are left out, so its scores are only comparable with other stub renders.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "slide_team"))
import imgcmp  # noqa: E402

RECT = re.compile(r"<rect ([^>]*?)/?>")
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
HEX = re.compile(r"#[0-9A-Fa-f]{6}$")


class RectRenderer:
    def __init__(self):
        self.calls = 0

    def render(self, svg_path, png_path, size):
        self.calls += 1
        w, h = size
        with open(svg_path, encoding="utf-8") as handle:
            svg = handle.read().split("</defs>", 1)[-1]  # the arrowhead markers are not drawn
        planes = [[bytearray(w) for _ in range(h)] for _ in range(3)]
        for found in RECT.finditer(svg):
            a = dict(ATTR.findall(found.group(1)))
            if not HEX.match(a.get("fill", "")):
                continue
            try:
                x, y, width, height = (float(a[k]) for k in ("x", "y", "width", "height"))
            except (KeyError, ValueError):
                continue
            x0, y0, x1, y1 = max(0, int(x)), max(0, int(y)), min(w, int(x + width)), min(h, int(y + height))
            if x1 <= x0 or y1 <= y0:
                continue
            for plane, value in zip(planes, (int(a["fill"][i:i + 2], 16) for i in (1, 3, 5))):
                span = bytes([value]) * (x1 - x0)
                for row in plane[y0:y1]:
                    row[x0:x1] = span
        imgcmp.write_png(png_path, (w, h, [(bytes(r), bytes(g), bytes(b)) for r, g, b in zip(*planes)]))
        return True
