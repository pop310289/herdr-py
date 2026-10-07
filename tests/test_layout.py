"""The layout team's program parts: component checks, reply parsing, picture comparison, keep-best and the lessons list."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "slide_team"))
import components as C  # noqa: E402
import imgcmp  # noqa: E402
import layout_team as L  # noqa: E402
import view  # noqa: E402

SIZE = (1206, 1441)


class CheckTest(unittest.TestCase):
    def test_defaults_are_filled_in(self):
        errors, warnings, out = C.check([{"id": "r1.p", "type": "tokens", "x": 50, "y": 265, "labels": ["P", "P"]}], "r1.", SIZE)
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual((out[0]["kind"], out[0]["w"], out[0]["gap"]), ("prompt", 44, 6))

    def test_errors_name_the_component_and_the_fix(self):
        errors, _, out = C.check([{"id": "r1.lane", "type": "lane", "x": 878, "w": 277, "label": "A"},
                                  {"id": "r1.t", "type": "tokens", "x": 1, "y": 1, "labels": ["A"], "kind": "token"},
                                  {"id": "r2.x", "type": "text", "x": 1, "y": 1, "text": "hi"},
                                  {"id": "r1.y", "type": "circle", "x": 1, "y": 1}], "r1.", SIZE)
        self.assertEqual(out, [])
        self.assertIn('r1.lane: missing "y"', errors)
        self.assertTrue(any(e.startswith("r1.t: kind \"token\" is not allowed; use one of prompt") for e in errors), errors)
        self.assertIn('r2.x: ids in this row must start with "r1."', errors)
        self.assertTrue(any(e.startswith('r1.y: unknown type "circle"; use one of text, tokens') for e in errors), errors)

    def test_canvas_size_and_value_types(self):
        errors = C.check([{"id": "r1.a", "type": "text", "x": 1300, "y": 10, "text": "hi"},
                          {"id": "r1.b", "type": "text", "x": True, "y": 10, "text": "hi"},
                          {"id": "r1.c", "type": "box", "x": 1000, "y": 10, "w": 300, "h": 20}], "r1.", SIZE)[0]
        self.assertEqual(errors, ["r1.a: x 1300, y 10 is outside the 1206 x 1441 canvas", 'r1.b: "x" must be a number',
                                  "r1.c: it goes past the right or bottom edge of the canvas"])
        self.assertEqual(C.check([{"id": "r1.a", "type": "text", "x": 1300, "y": 10, "text": "hi"}], "r1.")[0], [])  # 1920 x 1080

    def test_lane_tokens_must_sit_on_the_lane(self):
        lane = {"id": "r1.l", "type": "lane", "x": 878, "y": 300, "w": 277, "label": "A", "tokens": [{"at": 860, "text": "A"}]}
        self.assertEqual(C.check([lane], "r1.", SIZE)[0], ["r1.l: a token at 860 is not on the lane (x 878 to 1155)"])

    def test_unknown_settings_warn_and_overlapping_texts_warn(self):
        errors, warnings, _ = C.check([{"id": "r1.a", "type": "text", "x": 50, "y": 50, "text": "Long prompt", "font": "x"},
                                       {"id": "r1.b", "type": "text", "x": 60, "y": 55, "text": "Live streams"}], "r1.", SIZE)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ['r1.a: "font" is not a text setting and was ignored', "texts r1.a and r1.b overlap; move one of them"])

    def test_missing_labels_look_at_every_kind_of_text(self):
        comps = C.check([{"id": "r1.g", "type": "box", "x": 1, "y": 1, "w": 9, "h": 9, "title": "1 GPU"},
                         {"id": "r1.l", "type": "lane", "x": 878, "y": 300, "w": 277, "label": "New"}], "r1.", SIZE)[2]
        self.assertEqual(C.missing(comps, ["1 GPU", "new", "local"]), ["local"])


class ParseTest(unittest.TestCase):
    def test_json_block_bare_list_and_nothing(self):
        self.assertEqual(C.parse_reply('Thinking [about it]\n```json\n[{"id": "r1.a"}]\n```'), [{"id": "r1.a"}])
        self.assertEqual(C.parse_reply('Here: [{"id": "r1.a"}] done'), [{"id": "r1.a"}])
        self.assertIsNone(C.parse_reply("I drew the row."))
        self.assertIsNone(C.parse_reply("```json\n[{\"id\": }]\n```"))


def picture(w, h, boxes, background=(244, 247, 251)):
    """An RGB image as imgcmp holds it, with filled rectangles (x0, y0, x1, y1, (r, g, b))."""
    rows = []
    for y in range(h):
        line = [background] * w
        for x0, y0, x1, y1, rgb in boxes:
            if y0 <= y < y1:
                line[x0:x1] = [rgb] * (x1 - x0)
        rows.append(tuple(bytes(p[k] for p in line) for k in range(3)))
    return w, h, rows


ORANGE, GREEN, BLUE = (245, 166, 35), (76, 175, 80), (47, 128, 237)


class PictureTest(unittest.TestCase):
    def test_png_round_trip_and_crop(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        image = picture(40, 30, [(5, 5, 20, 15, ORANGE)])
        path = os.path.join(d, "a.png")
        imgcmp.write_png(path, image)
        self.assertEqual(imgcmp.read_png(path), image)
        self.assertEqual(imgcmp.crop(image, 5, 5, 20, 15)[:2], (15, 10))

    def test_colour_classes_of_the_palette(self):
        expect = {"#F5A623": "orange", "#4CAF50": "green", "#2F80ED": "blue", "#1B2430": "dark", "#D0505C": "red",
                  "#9AA4AE": "gray", "#EAF1F8": "light", "#FDEBD0": "peach", "#F4F7FB": None, "#FFFFFF": None}
        for hexa, cls in expect.items():
            rgb = tuple(int(hexa[i:i + 2], 16) for i in (1, 3, 5))
            self.assertEqual(imgcmp.colour(*rgb), cls, hexa)

    def test_match_rewards_the_same_layout_and_tolerates_a_small_shift(self):
        original = picture(160, 96, [(16, 16, 64, 48, ORANGE), (96, 16, 144, 80, BLUE)])
        self.assertEqual(imgcmp.compare(original, original)["match"], 1.0)
        shifted = picture(160, 96, [(24, 16, 72, 48, ORANGE), (104, 16, 152, 80, BLUE)])  # half a square to the right
        wrong = picture(160, 96, [(16, 16, 64, 48, GREEN), (96, 16, 144, 80, BLUE)])
        blank = picture(160, 96, [])
        m = {name: imgcmp.compare(img, original)["match"] for name, img in (("shifted", shifted), ("wrong", wrong), ("blank", blank))}
        self.assertGreater(m["shifted"], 0.8)
        self.assertLess(m["wrong"], m["shifted"])
        self.assertEqual(m["blank"], 0.0)
        self.assertLess(imgcmp.compare(blank, original)["psnr"], imgcmp.compare(shifted, original)["psnr"])

    def test_feedback_says_where_colour_is_missing_or_extra(self):
        original = picture(160, 96, [(16, 16, 64, 80, ORANGE)])
        ours = picture(160, 96, [(96, 16, 144, 80, BLUE)])
        r = imgcmp.compare(ours, original, regions=[("left", 0, 0, 80, 96), ("right", 80, 0, 160, 96)])
        notes = imgcmp.feedback(r, least=4)
        self.assertTrue(any(n.startswith("left: the original has more orange") for n in notes), notes)
        self.assertTrue(any(n.startswith("right: we have more blue") for n in notes), notes)

    def test_mask_leaves_squares_out(self):  # the only difference sits under the mask (the phone icons of the original)
        original = picture(96, 64, [(0, 0, 32, 64, ORANGE), (48, 0, 96, 64, BLUE)])
        ours = picture(96, 64, [(48, 0, 96, 64, BLUE)])
        masked = imgcmp.compare(ours, original, mask=lambda x, y: x < 32)
        self.assertEqual((masked["match"], masked["psnr"]), (1.0, float("inf")))
        self.assertLess(imgcmp.compare(ours, original)["match"], 1.0)


class TeamRulesTest(unittest.TestCase):
    def test_keep_best_needs_a_real_improvement(self):
        self.assertTrue(L.better({"score": 0.40}, None))
        self.assertTrue(L.better({"score": 0.41}, {"score": 0.40}))
        self.assertFalse(L.better({"score": 0.401}, {"score": 0.40}))
        self.assertFalse(L.better({"score": 0.39}, {"score": 0.40}))

    def test_a_missing_label_costs_more_than_a_small_picture_gain(self):
        self.assertLess(L.score_of(0.45, ["New"]), L.score_of(0.43, []))

    def test_lessons_generalise_the_component_and_count_repeats(self):
        lessons = L.Lessons()
        reply = [{"id": "r1.lane1", "type": "lane"}]
        self.assertTrue(lessons.add(L.lesson_of('r1.lane1: missing "y"', reply)))
        self.assertFalse(lessons.add(L.lesson_of('r1.lane1: missing "y"', reply)))
        self.assertEqual(lessons.text(), 'Mistakes already made in this run; do not repeat them:\n- a lane: missing "y" (2 times)')
        self.assertEqual(L.lesson_of("the reply had no ```json block with a JSON list", None), "the reply had no ```json block with a JSON list")
        self.assertEqual(L.Lessons().text(), "")

    def test_art_notes(self):
        self.assertEqual(L.art_notes("ok\nDIFF: a - b\nDIFF: c - d\nDIFF: e\nDIFF: f"), (["DIFF: a - b", "DIFF: c - d", "DIFF: e"], False))
        self.assertEqual(L.art_notes("SAME"), ([], True))


class ViewTest(unittest.TestCase):
    def test_a_daemon_that_times_out_does_not_stop_the_viewer(self):  # it crashed once when a run was stopped
        import socket

        class Slow:
            def call(self, method, **params):
                raise socket.timeout("timed out")
        self.assertEqual(view.fetch_agents(Slow()), {})

    def test_agents_file_for_codex_members(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "agents.json")
        with open(path, "w") as handle:
            handle.write('{"drawA": {"state": "idle"}}')
        self.assertEqual(view.fetch_agents(None, path), {"drawA": {"state": "idle"}})
        with open(path, "w") as handle:
            handle.write('{"drawA": ')  # half written
        self.assertEqual(view.fetch_agents(None, path), {})


if __name__ == "__main__":
    unittest.main()
