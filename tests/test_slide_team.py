"""slide_team.build: the checks must judge the slide that make_deck.py makes now, never a deck.json an earlier round left behind."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "slide_team"))
import slide_team  # noqa: E402


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work)
        self.deck = os.path.join(self.work, "deck.json")
        with open(self.deck, "w") as handle:  # what an earlier round left behind
            json.dump({"round": 1}, handle)

    def build(self, source, timeout=30):
        with open(os.path.join(self.work, "make_deck.py"), "w") as handle:
            handle.write(source)
        return slide_team.build(self.work, [sys.executable, "make_deck.py"], timeout=timeout)

    def test_a_working_program_replaces_the_old_deck(self):
        ok, text = self.build('import json\njson.dump({"round": 2}, open("deck.json", "w"))\n')
        self.assertTrue(ok, text)
        with open(self.deck) as handle:
            self.assertEqual(json.load(handle), {"round": 2})

    def test_a_syntax_error_leaves_no_old_deck_and_says_where(self):
        ok, text = self.build('deck = {"slides": [1, 2}\n')
        self.assertFalse(ok)
        self.assertIn("SyntaxError", text)
        self.assertIn("line 1", text)
        self.assertFalse(os.path.exists(self.deck))

    def test_a_program_that_writes_nothing_fails(self):
        ok, text = self.build('print("drew the slide")\n')
        self.assertFalse(ok)
        self.assertIn("did not write deck.json", text)
        self.assertFalse(os.path.exists(self.deck))

    def test_a_program_that_hangs_is_stopped(self):
        ok, text = self.build("import time\ntime.sleep(10)\n", timeout=1)
        self.assertFalse(ok)
        self.assertIn("did not finish", text)


class FindingsTest(unittest.TestCase):
    DECK = {"slides": [{"elements": [{"id": "title"}, {"id": "credit"}]}]}

    def test_names_the_element_and_splits_by_severity(self):  # the format `open_slide_py validate` printed on 2026-10-07
        out = json.dumps([{"severity": "warning", "code": "low_contrast", "path": "$.slides[0].elements[1]", "message": "contrast 3.6:1"},
                          {"severity": "error", "code": "geometry", "path": "$.slides[0].elements[0].width", "message": "positive dimensions"}])
        self.assertEqual(slide_team.findings(out, self.DECK),
                         (["geometry at title.width: positive dimensions"], ["low_contrast at credit: contrast 3.6:1"]))

    def test_an_index_that_is_not_in_the_deck_keeps_the_path(self):
        out = json.dumps([{"severity": "warning", "code": "text_overlap", "path": "$.slides[0].elements[7]", "message": "m"}])
        self.assertEqual(slide_team.findings(out, self.DECK), ([], ["text_overlap at $.slides[0].elements[7]: m"]))

    def test_output_that_is_not_a_list_of_findings_gives_none(self):
        self.assertIsNone(slide_team.findings("Traceback (most recent call last):", self.DECK))
        self.assertIsNone(slide_team.findings('{"severity": "error"}', self.DECK))
        self.assertIsNone(slide_team.findings('["error"]', self.DECK))


class NudgeTest(unittest.TestCase):
    def test_a_drawer_that_stopped_without_changing_the_file_is_sent_back(self):
        self.assertEqual(slide_team.nudge_reason("idle", False, True, "make_deck.py ran"), "make_deck.py is unchanged")

    def test_a_drawer_whose_program_fails_gets_the_error(self):
        self.assertEqual(slide_team.nudge_reason("idle", True, False, "make_deck.py stopped with an error: x"),
                         "make_deck.py stopped with an error: x")

    def test_finished_work_is_not_nudged(self):
        self.assertIsNone(slide_team.nudge_reason("idle", True, True, "make_deck.py ran"))

    def test_a_drawer_stopped_at_the_time_limit_is_not_nudged(self):
        self.assertIsNone(slide_team.nudge_reason("aborted", False, False, "x"))


if __name__ == "__main__":
    unittest.main()
