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


if __name__ == "__main__":
    unittest.main()
