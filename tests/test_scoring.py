"""The strict picture score (examples/slide_team/scoring.py), calibrated on synthetic slides drawn straight into pixels.

The reference slide has three rows like the real one: a dark label, small tokens with letters, a line into a box with a
border and a title, a grey lane with blue tokens. Row 2's box is the real case of 2026-10-07: a light fill with an orange
border and an orange title. Each degraded copy must score clearly lower than the reference, and the reference must score
highest; the border-and-title recolour must leave match where it was while the strict score drops."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "slide_team"))
import imgcmp  # noqa: E402
import scoring  # noqa: E402

BG, DARK, SLATE, GREY, WHITE = (244, 247, 251), (27, 36, 48), (107, 122, 140), (154, 164, 174), (255, 255, 255)
ORANGE, ORANGE_LINE, ORANGE_TEXT, PEACH = (245, 166, 35), (199, 124, 2), (154, 91, 0), (253, 235, 208)
BLUE, BLUE_TEXT, LIGHT_BLUE = (47, 128, 237), (27, 95, 184), (220, 235, 252)
GREEN, GREEN_LINE, LIGHT = (76, 175, 80), (46, 125, 50), (226, 230, 236)
FONT = {"E": ("#####", "#....", "####.", "#....", "#....", "#....", "#####"),
        "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
        "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
        "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
        "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
        "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#..")}
W, H, ROW = 520, 350, 110
ROWS = {n: (ROW * (n - 1), ROW * n) for n in (1, 2, 3)}
ZONES = (("left", 0, 140), ("middle", 140, 350), ("right", 350, W))


class Sketch:
    """An RGB picture as imgcmp holds it, drawn with filled rectangles, bordered boxes and 5 x 7 block letters."""

    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.px = [[bytearray([c]) * w for c in BG] for _ in range(h)]

    def rect(self, x0, y0, x1, y1, rgb):
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.w, x1), min(self.h, y1)
        for y in range(y0, y1):
            for channel, value in zip(self.px[y], rgb):
                channel[x0:x1] = bytes([value]) * max(0, x1 - x0)

    def box(self, x0, y0, x1, y1, fill, border, width=3):
        self.rect(x0, y0, x1, y1, border)
        self.rect(x0 + width, y0 + width, x1 - width, y1 - width, fill)

    def word(self, x, y, text, rgb, scale=3):
        for i, ch in enumerate(text):
            for r, line in enumerate(FONT[ch]):
                for c, bit in enumerate(line):
                    if bit == "#":
                        self.rect(x + (6 * i + c) * scale, y + r * scale, x + (6 * i + c + 1) * scale, y + (r + 1) * scale, rgb)

    def image(self):
        return self.w, self.h, [tuple(bytes(channel) for channel in line) for line in self.px]


def row(n, box=(LIGHT, ORANGE_LINE, ORANGE_TEXT), tokens=(LIGHT, GREY, DARK), line=ORANGE, dx=0, dy=0):
    """One row as drawing steps: label, four lettered tokens, a line into a box with a title, a lane with blue tokens."""
    y = 15 + ROW * (n - 1) + dy
    fill, border, letter = tokens
    steps = [("word", 12 + dx, y, "HELP", DARK)]
    for i in range(4):
        steps += [("box", 12 + 32 * i + dx, y + 40, 38 + 32 * i + dx, y + 66, fill, border, 2),
                  ("word", 20 + 32 * i + dx, y + 46, "P", letter, 2)]
    steps += [("rect", 140 + dx, y + 52, 208 + dx, y + 55, line),
              ("box", 208 + dx, y + 5, 334 + dx, y + 93) + box[:2],
              ("word", 236 + dx, y + 16, "TOP", box[2], 2),
              ("rect", 360 + dx, y + 55, 470 + dx, y + 57, GREY)]
    steps += [("box", 375 + 36 * i + dx, y + 43, 401 + 36 * i + dx, y + 69, BLUE, BLUE, 2) for i in range(3)]
    return steps


REFERENCE = {1: row(1, box=(LIGHT_BLUE, BLUE, BLUE_TEXT), tokens=(BLUE, BLUE_TEXT, WHITE), line=GREY),
             2: row(2),
             3: row(3, box=(LIGHT, SLATE, DARK), tokens=(GREEN, GREEN_LINE, WHITE), line=GREY)}


def slide(**rows):
    """The reference slide with some rows replaced (row2=[...]) or removed (row2=None)."""
    plan = dict(REFERENCE)
    for name, steps in rows.items():
        plan[int(name[3:])] = steps
    sketch = Sketch()
    for n in sorted(plan):
        for step in plan[n] or ():
            getattr(sketch, step[0])(*step[1:])
    return sketch.image()


def row2_regions():
    y0, y1 = ROWS[2]
    return [(f"row 2 {zone} (x {x0}-{x1})", x0, y0, x1, y1) for zone, x0, x1 in ZONES]


class InkTest(unittest.TestCase):
    def test_ink_families_of_the_palette_and_of_the_original(self):
        expect = {"orange": [ORANGE, ORANGE_LINE, ORANGE_TEXT, (135, 118, 87), (161, 128, 92), (100, 65, 31), (95, 66, 36),
                             (190, 200, 40)],
                  "blue": [BLUE, BLUE_TEXT, (151, 191, 246), (40, 200, 190)],
                  "green": [GREEN, GREEN_LINE],
                  "red": [(208, 80, 92), (160, 80, 64), (152, 80, 56)],
                  "dark": [DARK, (93, 102, 115), SLATE, GREY, (171, 192, 204), (197, 204, 211), (94, 67, 36), (80, 56, 32)],
                  None: [PEACH, LIGHT_BLUE, LIGHT, BG, WHITE, (234, 241, 248), (233, 236, 239)]}
        # measured on the original: (135, 118, 87) ... (95, 66, 36) its brown Prefill border and title, (80, 56, 32) the
        # near-black brown letters of its prompt tokens (below 95 everywhere: dark, as in imgcmp.colour), (160, 80, 64) its
        # red WAIT, (171, 192, 204) its blue-grey lines; yellow joins orange and cyan joins blue
        for family, colours in expect.items():
            for rgb in colours:
                self.assertEqual(scoring.ink(*rgb), family, rgb)

    def test_the_ink_of_an_anti_aliased_stroke_is_its_core(self):  # like the original's prompt-token letters
        sketch = Sketch(48, 48)
        sketch.rect(8, 8, 40, 40, LIGHT)
        sketch.rect(20, 12, 25, 36, (108, 90, 73))  # the blended edge pixels are orange by themselves
        sketch.rect(21, 12, 24, 36, (80, 56, 32))   # the core is near-black brown
        self.assertEqual(scoring.ink(108, 90, 73), "orange")
        grid = scoring.inks(sketch.image())
        self.assertEqual({k for line in grid for square in line if square for k in square[1]}, {"dark"})

    def test_a_colour_that_does_not_count_in_the_other_square_cannot_match(self):
        squares = ({"orange": 1}, {"orange": 2}, {"orange": 2, "dark": 7}, {"orange": 3, "dark": 9})
        self.assertEqual([scoring.counted((False, c)) for c in squares],  # at least 2 samples and a quarter of the square's
                         [[], ["orange"], ["dark"], ["orange", "dark"]])
        ours, original = [[(False, {"orange": 16})]], [[(False, {"dark": 18, "orange": 2})]]
        parts, tally, misses = scoring.ink_parts(ours, original)
        self.assertEqual((parts["stroke"], tally["stroke"]), (0.0, {"orange": [0, 1], "dark": [0, 1]}))
        self.assertEqual(misses, [(0, 0, "stroke", "dark", "orange")])

    def test_a_few_darker_pixels_in_a_stroke_do_not_count_as_a_colour(self):  # the original's brown strokes vary in shade
        plain, varied = Sketch(160, 48), Sketch(160, 48)
        for sketch in (plain, varied):
            sketch.rect(0, 20, 160, 23, ORANGE_LINE)
        for x in range(4, 160, 16):
            varied.rect(x, 20, x + 1, 23, (80, 56, 32))  # near-black brown: dark
        self.assertEqual(scoring.inks(varied.image())[1][1], (False, {"orange": 16, "dark": 2}))
        r = scoring.compare(varied.image(), plain.image())
        self.assertEqual((r["stroke"], set(r["tally"]["stroke"])), (1.0, {"orange"}))

    def test_lettering_and_borders_go_to_different_parts(self):
        sketch = Sketch(160, 64)
        sketch.word(8, 8, "HELLO", DARK)
        sketch.rect(0, 50, 160, 53, ORANGE_LINE)
        grid = scoring.inks(sketch.image())
        word = [grid[cy][cx] for cy in range(0, 2) for cx in range(0, 5) if grid[cy][cx]]
        line = [grid[3][cx] for cx in range(10) if grid[3][cx]]
        self.assertGreaterEqual(sum(1 for square in word if square[0]), 6)
        self.assertTrue(all(square[1].get("dark") for square in word))
        self.assertEqual([square[0] for square in line], [False] * 10)
        self.assertTrue(all(set(square[1]) == {"orange"} for square in line))


class CalibrationTest(unittest.TestCase):
    """Degraded copies of the reference slide against the reference itself."""

    @classmethod
    def setUpClass(cls):
        cls.reference = slide()
        cls.prepared = scoring.prepare(cls.reference)

    def score(self, image, row=None, regions=()):
        box = (0, ROWS[row][0], W, ROWS[row][1]) if row else None
        return scoring.compare(image, self.reference, box=box, regions=regions, prepared=self.prepared)

    def test_the_reference_scores_one_in_every_part(self):
        r = self.score(self.reference)
        self.assertEqual([r[k] for k in ("match", "fill", "stroke", "text", "strict", "score")], [1.0] * 6)

    def test_a_few_pixels_off_is_tolerated_like_match(self):  # within one square, as match tolerates it
        r = self.score(slide(row2=row(2, dx=6)), row=2)
        self.assertEqual((r["stroke"], r["text"]), (1.0, 1.0))
        self.assertGreater(r["match"], 0.9)

    def test_border_and_title_turned_blue_leave_match_alone_and_lower_strict(self):  # the real case
        r = self.score(slide(row2=row(2, box=(LIGHT, BLUE, BLUE_TEXT))), row=2)
        self.assertGreaterEqual(r["match"], 0.99)
        self.assertLess(r["stroke"], 0.85)
        self.assertLess(r["text"], 0.85)
        self.assertLess(r["strict"], 0.88)

    def test_strict_rejects_the_real_runs_revision_that_match_accepted(self):
        draft = self.score(slide(row2=row(2, box=(PEACH, ORANGE_LINE, ORANGE_TEXT))), row=2)  # wrong fill, right strokes
        revision = self.score(slide(row2=row(2, box=(LIGHT_BLUE, BLUE, BLUE_TEXT))), row=2)  # light fill, blue strokes
        self.assertGreater(revision["match"], draft["match"] + 0.2)
        self.assertGreater(draft["strict"], revision["strict"] + 0.04)

    def test_every_degradation_scores_clearly_lower_and_the_reference_highest(self):
        degraded = {"border and title blue": slide(row2=row(2, box=(LIGHT, BLUE, BLUE_TEXT))),
                    "row shifted 40 px": slide(row2=row(2, dx=40)),
                    "row removed": slide(row2=None),
                    "fills recoloured": slide(row2=row(2, box=(PEACH, ORANGE_LINE, ORANGE_TEXT), tokens=(GREEN, GREY, DARK)))}
        best = self.score(self.reference, row=2)["strict"]
        for name, image in degraded.items():
            for row_box in (2, None):  # the row the layout team scores, and the whole slide
                with self.subTest(name=name, row=row_box):
                    self.assertLess(self.score(image, row=row_box)["strict"], best - 0.05)
        self.assertLess(self.score(degraded["row removed"], row=2)["strict"], 0.5)

    def test_feedback_names_the_region_and_both_colours(self):
        r = self.score(slide(row2=row(2, box=(LIGHT, BLUE, BLUE_TEXT))), row=2, regions=row2_regions())
        notes = scoring.feedback(r, limit=4)
        self.assertTrue(notes[0].startswith("row 2 middle (x 140-350): the original's borders, lines and text are orange (prefill), "
                                            "ours are blue (decode) ("), notes)
        self.assertTrue(notes[0].endswith("squares): change them to orange (prefill)"), notes)

    def test_feedback_puts_the_costliest_colour_first(self):  # 6 of 12 orange squares cost more than 10 of 200 dark ones
        result = {"tally": {"stroke": {"orange": [6, 12], "dark": [190, 200], "blue": [40, 50]}, "text": {"orange": [4, 8]}},
                  "ink_misses": [("row 3 middle", [(1, 1, "stroke", "dark", "blue")] * 10 + [(2, 1, "stroke", "orange", "blue")] * 6
                                  + [(4, 1, "text", "orange", "dark")] * 4 + [(3, 1, "text", "dark", None)] * 3)]}
        self.assertEqual(scoring.ink_feedback(result), [
            "row 3 middle: the original's borders and lines are orange (prefill), ours are blue (decode) (6 squares): "
            "change them to orange (prefill)",
            "row 3 middle: the original's text is orange (prefill), ours is dark (black or grey) (4 squares): "
            "change them to orange (prefill)",
            "row 3 middle: the original's borders and lines are dark (black or grey), ours are blue (decode) (10 squares): "
            "change them to dark (black or grey)"])  # the 3 squares of missing dark text are below the least of 4

    def test_feedback_for_missing_and_extra_ink(self):
        r = self.score(slide(row2=None), row=2, regions=row2_regions())
        self.assertTrue(any(n.startswith("row 2 middle (x 140-350): the original has orange (prefill) borders") for n in
                            scoring.ink_feedback(r)), scoring.ink_feedback(r))
        extra = Sketch()
        extra.rect(150, 160, 200, 163, (208, 80, 92))  # a red line where the reference has nothing
        r = scoring.compare(extra.image(), slide(row1=None, row2=None, row3=None), regions=row2_regions())
        self.assertEqual(scoring.ink_feedback(r), ["row 2 middle (x 140-350): ours has red borders and lines where the original "
                                                   "has none (8 squares): remove or move them"])


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.reference = slide()
        self.ours = slide(row2=row(2, box=(LIGHT, BLUE, BLUE_TEXT)))

    def test_match_mode_keeps_todays_score_and_notes(self):
        r = scoring.compare(self.ours, self.reference, regions=row2_regions(), score_mode="match")
        self.assertEqual(r["score"], r["match"])
        self.assertEqual(r["match"], imgcmp.compare(self.ours, self.reference, regions=row2_regions())["match"])
        self.assertEqual(scoring.feedback(r, limit=4), imgcmp.feedback(r, limit=4))
        self.assertLess(r["strict"], r["match"])  # the parts are there to be logged before the switch

    def test_strict_mode_scores_strict_and_puts_colour_notes_first(self):
        r = scoring.compare(self.ours, self.reference, regions=row2_regions(), score_mode="strict")
        self.assertEqual(r["score"], r["strict"])
        both = slide(row2=row(2, box=(PEACH, BLUE, BLUE_TEXT)))  # wrong strokes and a wrong fill
        notes = scoring.feedback(scoring.compare(both, self.reference, regions=row2_regions()), limit=4)
        self.assertIn("ours are blue (decode)", notes[0])
        self.assertTrue(any("light orange box fill" in n for n in notes[1:]), notes)
        with self.assertRaises(ValueError):
            scoring.compare(self.ours, self.reference, score_mode="strictest")
        with self.assertRaises(ValueError):
            scoring.compare(Sketch(W + 32, H).image(), self.reference)

    def test_mask_leaves_squares_out_of_the_ink_parts(self):  # the only difference sits under the mask
        r = scoring.compare(self.ours, self.reference, mask=lambda x, y: 200 <= x < 340 and 110 <= y < 220)
        self.assertEqual((r["match"], r["stroke"], r["text"]), (1.0, 1.0, 1.0))

    def test_the_switch_as_layout_team_would_use_it(self):
        """layout_team's canvas, mask, row 3 box, regions, score_of and keep-best rule: in match mode the wrong revision of
        2026-10-07 replaces the draft, in strict mode it does not."""
        import layout_team as L

        def picture(box):
            sketch = Sketch(*L.SIZE)
            for step in row(3, box=box, dx=300, dy=L.ROWS[3][0] - 235):
                getattr(sketch, step[0])(*step[1:])
            return sketch.image()
        original = picture((LIGHT, ORANGE_LINE, ORANGE_TEXT))
        draft, revision = picture((PEACH, ORANGE_LINE, ORANGE_TEXT)), picture((LIGHT_BLUE, BLUE, BLUE_TEXT))
        prepared = scoring.prepare(original, L.phone_icons)
        y0, y1 = L.ROWS[3]
        regions = [(f"row 3 {zone} (x {x0}-{x1})", x0, y0, x1, y1) for zone, x0, x1 in L.ZONES]
        kept = {}
        for mode in scoring.SCORE_MODES:
            ev = {}
            for name, image in (("draft", draft), ("revision", revision)):
                r = scoring.compare(image, original, L.phone_icons, box=(0, y0, L.SIZE[0], y1), regions=regions,
                                    prepared=prepared, score_mode=mode)
                ev[name] = {"score": L.score_of(r["score"], []), "notes": scoring.feedback(r, limit=4)}
            kept[mode] = "revision" if L.better(ev["revision"], ev["draft"]) else "draft"
            if mode == "strict":
                self.assertTrue(ev["revision"]["notes"][0].startswith("row 3 middle (x 370-780): the original's borders, lines "
                                                                      "and text are orange (prefill), ours are blue (decode)"))
        self.assertEqual(kept, {"match": "revision", "strict": "draft"})


if __name__ == "__main__":
    unittest.main()
