#!/usr/bin/env python3
"""Calibrate the strict score (scoring.py) on real renders: draw the reference layout and degraded copies of it with
headless Chrome, then score every copy row by row against the original picture and against the reference render.

Degraded copies: row 3's Prefill box with a blue border and title (fill kept), the change of the real run on 2026-10-07
(that box and its cells turned to the decode kind), row 1's grey prompt tokens drawn orange (the original's prompt letters
are near-black brown, so a colour score could be lured into preferring them), each row shifted 40 px to the right, each
row removed, and each row's fills recoloured (borders and text kept). Palette entries for the recoloured kinds are added
in this process only.
--renders scores more whole-slide PNGs against the original, e.g. a run's renders/NN-rowN-*.png.
usage: scoring_calibrate.py --original ORIGINAL.png [--out DIR] [--chrome PATH] [--renders PNG ...]
"""
import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import components as C  # noqa: E402
import imgcmp  # noqa: E402
import scoring  # noqa: E402
import slide_team  # noqa: E402

SIZE = (1206, 1441)
ROWS = {1: (152, 558), 2: (558, 960), 3: (960, 1400)}  # as in layout_team.py


def phone_icons(x, y):  # as in layout_team.py
    return x >= 1075 and y >= 965


def add_calibration_kinds():
    """Palette entries that only degraded copies use: a box with the prefill fill but decode border and title, and every
    token and box kind with its fill swapped for another family's."""
    C.BOXES["prefill_blue_lines"] = (C.BOXES["prefill"][0],) + C.BOXES["decode"][1:]
    refill = {"prompt": "#FDEBD0", "prefill": "#4CAF50", "kv": "#2F80ED", "decode": "#F5A623", "empty": "#DCEBFC", "outline": "#FDEBD0"}
    for kind, fill in refill.items():
        C.TOKENS[kind + "_refill"] = (fill,) + C.TOKENS[kind][1:]
    for kind, fill in {"gpu": "#FDEBD0", "pool": "#DCEBFC", "decode": "#FDEBD0", "chunk": "#DCEBFC", "prefill": "#DCEBFC"}.items():
        C.BOXES[kind + "_refill"] = (fill,) + C.BOXES[kind][1:]


def in_row(c, n):
    return c["id"].startswith(f"r{n}.")


def shifted(comps, n, dx):
    out = copy.deepcopy(comps)
    for c in out:
        if in_row(c, n):
            if "x" in c:
                c["x"] += dx
            if c["type"] == "arrow":
                c["points"] = [[p[0] + dx, p[1]] for p in c["points"]]
            if c["type"] == "lane":
                c["tokens"] = [dict(t, at=t["at"] + dx) for t in c["tokens"]]
    return out


def refilled(comps, n):
    out = copy.deepcopy(comps)
    default = {"tokens": "prompt", "grid": "kv", "box": "gpu"}
    for c in out:
        if in_row(c, n) and c["type"] in default:
            c["kind"] = c.get("kind", default[c["type"]]) + "_refill"
        if in_row(c, n) and c["type"] == "lane":
            c["tokens"] = [dict(t, kind=t.get("kind", "decode") + "_refill") for t in c["tokens"]]
    return out


def edited(comps, changes):
    out = copy.deepcopy(comps)
    for c in out:
        c.update(changes.get(c["id"], {}))
    return out


def variants(ref):
    out = [("reference", ref),
           ("row 3 Prefill box: border and title blue", edited(ref, {"r3.pre": {"kind": "prefill_blue_lines"}})),
           ("row 3 the real run's change", edited(ref, {"r3.pre": {"kind": "decode"}, "r3.pcells": {"kind": "decode"}})),
           ("row 1 prompt tokens as prefill kind", edited(ref, {"r1.prompt": {"kind": "prefill"}}))]  # brown letters lure
    out += [(f"row {n} shifted 40 px", shifted(ref, n, 40)) for n in ROWS]
    out += [(f"row {n} removed", [c for c in ref if not in_row(c, n)]) for n in ROWS]
    out += [(f"row {n} fills recoloured", refilled(ref, n)) for n in ROWS]
    return out


def render(a, base, name, comps):
    errors, _, ok = C.check(base + comps, size=SIZE)
    if errors:
        raise SystemExit(f"{name}: {errors[:3]}")
    stem = os.path.join(a.out, "".join(ch if ch.isalnum() else "_" for ch in name))
    with open(stem + ".svg", "w", encoding="utf-8") as handle:
        handle.write(C.svg(ok, size=SIZE))
    if not slide_team.screenshot(a.chrome, stem + ".svg", stem + ".png", os.path.join(a.out, ".chrome-profile"), size=SIZE):
        raise SystemExit(f"{name}: Chrome did not write the screenshot")
    return stem + ".png"


def table(title, pngs, target, prepared):
    print(f"\n== against {title}: per row match / stroke / text / strict")
    print(f"{'':44}" + "".join(f"{'row ' + str(n):<29}" for n in ROWS))
    for name, png in pngs:
        image, line = imgcmp.read_png(png), []
        for n, (y0, y1) in ROWS.items():
            r = scoring.compare(image, target, phone_icons, box=(0, y0, SIZE[0], y1), prepared=prepared)
            line.append(f"{r['match']:.3f} {r['stroke']:.3f} {r['text']:.3f} {r['strict']:.3f}")
        print(f"{name[:43]:44}" + "".join(f"{cell:<29}" for cell in line))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original", required=True, help="the original picture, 1206 x 1441 PNG")
    ap.add_argument("--out", default="scoring-calibration", help="folder for the SVG and PNG renders")
    ap.add_argument("--chrome", default=slide_team.CHROME)
    ap.add_argument("--renders", nargs="*", default=[], help="more whole-slide PNGs to score against the original")
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    add_calibration_kinds()
    layout = os.path.join(HERE, "layout")
    base = json.load(open(os.path.join(layout, "base_portrait.json"), encoding="utf-8"))
    ref = json.load(open(os.path.join(layout, "reference_portrait.json"), encoding="utf-8"))
    original = imgcmp.read_png(a.original)
    if original[:2] != SIZE:
        raise SystemExit(f"the original must be {SIZE[0]} x {SIZE[1]} pixels, got {original[0]} x {original[1]}")
    t = time.time()
    pngs = [(name, render(a, base, name, comps)) for name, comps in variants(ref)]
    print(f"rendered {len(pngs)} slides in {time.time() - t:.0f} s")
    t = time.time()
    table("the original", pngs + [(os.path.basename(p), p) for p in a.renders], original, scoring.prepare(original, phone_icons))
    reference = imgcmp.read_png(pngs[0][1])
    table("the reference render", pngs, reference, scoring.prepare(reference, phone_icons))
    print(f"\nscored in {time.time() - t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
