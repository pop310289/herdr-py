"""Lessons (herdr_py/lessons.py): a correction becomes a lesson that is told before an action where its `when` matches,
checked against what was handed in, and reviewed by fixed rules. The loop here runs with program members, so no model
is called: a run makes a mistake; a person records the lesson; the next runs are told and make it again; the review
retires the lesson, and the run after that is no longer told."""
import collections
import contextlib
import io
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from herdr_py import engineview, lessons, notebook, rundiag  # noqa: E402
from herdr_py.lessons import CONFIRM_AFTER, RETIRE_AFTER, LessonError, Lessons  # noqa: E402
import test_engine  # noqa: E402
import test_notebook  # noqa: E402

SCREENSHOT = dict(when=r"screenshot", wrong="took the screenshot with headless Chrome --window-size, which has a minimum width",
                  right="use phone-check: it emulates a 390 px phone", detect=r"--window-size")
ONE_BY_ONE = dict(when=r"number as high", wrong="planned to try the numbers one by one",
                  right="start from the best answer so far and raise it", detect=r"one by one")


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(test_engine.tempfile.mkdtemp(), "lessons.jsonl")
        self.addCleanup(test_engine.shutil.rmtree, os.path.dirname(self.path), True)

    def test_a_lesson_is_added_once_and_checked(self):
        book = Lessons(self.path)
        first = book.add(by="person", **SCREENSHOT)
        self.assertEqual((first["status"], first["id"][0], len(first["id"])), ("hypothesis", "L", 9))
        self.assertEqual(book.add(by="someone else", **SCREENSHOT)["id"], first["id"])  # the same lesson again
        with open(self.path) as handle:
            self.assertEqual(len(handle.readlines()), 1)
        for bad, why in ((dict(SCREENSHOT, when="(unclosed"), "not a regular expression"),
                         (dict(SCREENSHOT, detect="[a-"), "not a regular expression"),
                         (dict(SCREENSHOT, when=""), "give a regular expression"),
                         (dict(SCREENSHOT, wrong=" "), "needs what went wrong"),
                         (dict(SCREENSHOT, right=""), "needs what went wrong")):
            with self.assertRaises(LessonError) as caught:
                book.add(by="person", **bad)
            self.assertIn(why, str(caught.exception))
        with self.assertRaises(LessonError):
            book.add(by="", **dict(SCREENSHOT, wrong="other"))

    def test_matching_ignores_case_and_retired_lessons(self):
        book = Lessons(self.path)
        a = book.add(by="p", **SCREENSHOT)
        b = book.add(by="p", **ONE_BY_ONE)
        self.assertEqual([l["id"] for l in book.matching("Take a SCREENSHOT of the page")], [a["id"]])
        self.assertEqual(book.matching("count the words"), [])
        with open(self.path, "a") as handle:
            handle.write(json.dumps({"type": "review", "id": a["id"], "t": 1, "status": "retired", "why": "test"}) + "\n")
            handle.write("not json\n")
        again = Lessons(self.path)
        self.assertEqual(again.matching("a screenshot of a number as high as possible"), [again.lessons[b["id"]]])
        self.assertEqual(again.bad_lines, 1)
        self.assertIn(b["right"], lessons.shown_text([again.lessons[b["id"]]]))
        self.assertEqual(lessons.shown_text([]), "")
        self.assertEqual(lessons.repeated([again.lessons[b["id"]]], ["try them ONE BY ONE", "x"]), [b["id"]])
        self.assertEqual(lessons.repeated([again.lessons[b["id"]]], ["start from the best"]), [])

    def test_the_review_rules_on_both_sides_of_each_threshold(self):
        book = Lessons(self.path)
        with_detect = book.add(by="p", **SCREENSHOT)
        without = book.add(by="p", **dict(SCREENSHOT, detect=None, wrong="another mistake"))

        def decide(lesson, shown, repeated):
            return lessons.decide(lesson, {"shown": shown, "repeated": repeated, "before": 0, "before_made": 0})[0]
        self.assertEqual(decide(with_detect, CONFIRM_AFTER - 1, 0), "hypothesis")
        self.assertEqual(decide(with_detect, CONFIRM_AFTER, 0), "confirmed")
        self.assertEqual(decide(with_detect, CONFIRM_AFTER, 1), "hypothesis")
        self.assertEqual(decide(with_detect, 10, RETIRE_AFTER - 1), "hypothesis")
        self.assertEqual(decide(with_detect, RETIRE_AFTER, RETIRE_AFTER), "retired")
        self.assertEqual(decide(without, 10, 0), "hypothesis")  # nothing to measure it by
        self.assertIn("no detect", lessons.decide(without, {"shown": 10, "repeated": 0})[1])
        # only forward: fewer runs never move a lesson back, and retired is for good
        self.assertEqual(decide(dict(with_detect, status="confirmed"), 1, 0), "confirmed")
        self.assertEqual(decide(dict(with_detect, status="confirmed"), 9, RETIRE_AFTER), "retired")
        self.assertEqual(decide(dict(with_detect, status="retired"), 0, 0), "retired")
        self.assertEqual(decide(dict(with_detect, status="retired"), CONFIRM_AFTER, 0), "retired")

    def test_counts_before_and_after_the_lesson(self):
        book = Lessons(self.path)
        lesson = book.add(by="p", **SCREENSHOT)
        t = lesson["t"]
        turns = [{"t": t - 9, "text": "take a screenshot", "made": ["chrome --window-size=390"], "shown": [], "repeated": []},
                 {"t": t - 8, "text": "take a screenshot", "made": ["phone-check"], "shown": [], "repeated": []},
                 {"t": t - 7, "text": "count words", "made": ["--window-size"], "shown": [], "repeated": []},
                 {"t": t + 1, "text": "take a screenshot", "made": ["x"], "shown": [lesson["id"]], "repeated": []},
                 {"t": t + 2, "text": "take a screenshot", "made": ["x"], "shown": [lesson["id"]], "repeated": [lesson["id"]]},
                 {"t": t + 3, "text": "take a screenshot", "made": ["--window-size"], "shown": [], "repeated": []}]
        self.assertEqual(lessons.measure(lesson, turns), {"shown": 2, "repeated": 1, "before": 2, "before_made": 1})


class LoopTest(test_engine.Base):
    def setUp(self):
        super().setUp()
        self.book_path = os.path.join(self.dir, "lessons.jsonl")
        self.runs = []

    def team_run(self, n):
        """One run: the planner adds a screenshot todo for a and a counting todo for b, and plans "one by one"; a hands
        in a screenshot made with --window-size (the mistake), b hands in a number."""
        self.script([test_engine.add({"text": "take a screenshot of the result", "for": "a", "parents": []},
                                     {"text": "count to ten one by one", "for": "b", "parents": []}),
                     test_engine.add(), test_engine.add(done=True)],
                    {"a": [{"answer": "ARTIFACT: data\nchrome --headless --window-size=390,800 shot.png"}],
                     "b": [{"answer": "7"}]})
        if os.path.isdir(self.log):
            test_engine.shutil.rmtree(self.log)
        self.out = os.path.join(self.dir, "run%d" % n)
        self.runs.append(self.out)
        extra = ["--lessons", self.book_path] if os.path.exists(self.book_path) else []
        code, said, err = self.run_main(*extra)
        self.assertEqual(code, 0, said + err)
        return said

    def turns(self):
        return {r["member"]: r for r in self.records("run.jsonl")}

    def test_told_repeated_retired(self):
        self.team_run(0)  # before any lesson: the mistake is made, and nobody is told anything
        self.assertNotIn("lessons", json.dumps(self.records("run.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "lessons.json")))
        with open(os.path.join(self.out, "task.md")) as handle:
            self.assertEqual(handle.read(), "Give a number as high as you can.\n")
        time.sleep(0.01)
        book = Lessons(self.book_path)
        shot = book.add(by="person", **SCREENSHOT)["id"]
        plan = book.add(by="supervisor", **ONE_BY_ONE)["id"]
        never = book.add(by="person", when="no task says this", wrong="w", right="r", detect="x")["id"]
        old = book.add(by="person", when="number", wrong="an old mistake", right="r", detect="x")["id"]
        with open(self.book_path, "a") as handle:
            handle.write(json.dumps({"type": "review", "id": old, "t": time.time(), "status": "retired", "why": "test"}) + "\n")

        said = self.team_run(1)
        turns = self.turns()
        self.assertEqual(turns["a"]["lessons"], [shot, plan])  # its todo says screenshot, the task says number as high
        self.assertEqual(turns["a"]["lessons_repeated"], [shot])
        self.assertEqual(turns["b"]["lessons"], [plan])
        self.assertNotIn("lessons_repeated", turns["b"])
        a_prompt, b_prompt = self.read_log("a-01.prompt"), self.read_log("b-01.prompt")
        self.assertIn("CORRECTIONS FROM EARLIER WORK", a_prompt)
        self.assertIn(SCREENSHOT["right"], a_prompt)
        self.assertNotIn(SCREENSHOT["right"], b_prompt)
        wakes = [w for w in self.records("engine.jsonl") if w.get("kind") == "wake"]
        self.assertTrue(all(w["lessons"] == [plan] for w in wakes))
        self.assertEqual([w.get("lessons_repeated") for w in wakes if w.get("added")], [[plan]])  # its todo says one by one
        self.assertIn(ONE_BY_ONE["right"], self.read_log("planner-01.txt"))
        for text in (json.dumps(self.records("run.jsonl")), json.dumps(wakes), a_prompt):
            self.assertNotIn(never, text)
            self.assertNotIn(old, text)  # retired: never told
        with open(os.path.join(self.out, "lessons.json")) as handle:
            self.assertEqual([l["id"] for l in json.load(handle)], [shot, plan, never])
        s = self.summary()["lessons"]
        self.assertEqual(s["repeated"], {shot: 1, plan: 1})
        self.assertEqual(s["turns_told"], 2 + len(wakes))
        self.assertIn("the corrected mistake came back in 2 (%s)" % ", ".join(sorted([shot, plan])), said)
        page = engineview.render(self.out)
        self.assertIn("Corrections from earlier work", page)
        self.assertIn("made a corrected mistake again: " + shot, page)
        self.assertIn("not told: no task or todo matched", page)  # the lesson nothing matched
        found = {f["code"]: f for f in rundiag.findings(self.out)}
        self.assertEqual(found["lesson_repeated"]["numbers"], {"turns": 2, "lessons": {shot: 1, plan: 1}})

        review = {l["id"]: (c, status) for l, c, status, _ in Lessons(self.book_path).review(self.runs[:2])}
        self.assertEqual(review[shot], ({"shown": 1, "repeated": 1, "before": 1, "before_made": 1}, "hypothesis"))
        self.assertEqual(review[never][0]["shown"], 0)

        self.team_run(2)  # told again, and the mistakes again: the second time retires both lessons
        review = {l["id"]: (c, status) for l, c, status, _ in Lessons(self.book_path).review(self.runs)}
        self.assertEqual(review[shot], ({"shown": 2, "repeated": 2, "before": 1, "before_made": 1}, "retired"))
        self.assertEqual(review[plan][1], "retired")  # the planner's "one by one" todo, in both runs
        self.assertEqual(review[plan][0]["before_made"], 1)  # run 0's planner added that todo before the lesson existed
        self.assertEqual((review[old][1], review[never][1]), ("retired", "hypothesis"))  # retired stays retired
        book = Lessons(self.book_path)
        self.assertEqual([book.lessons[i]["status"] for i in (shot, plan, never, old)],
                         ["retired", "retired", "hypothesis", "retired"])  # the review was recorded

        self.team_run(3)
        self.assertNotIn("lessons", self.turns()["a"])  # a retired lesson is not told any more
        self.assertNotIn(SCREENSHOT["right"], self.read_log("a-01.prompt"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lessons.main([self.book_path, "list"]), 0)
        self.assertIn(shot + "  retired", out.getvalue())

    def test_every_version_a_repaired_turn_handed_in_is_checked(self):
        Lessons(self.book_path).add(by="person", **SCREENSHOT)
        self.script([test_engine.add({"text": "take a screenshot of the result", "for": "a", "parents": []}),
                     test_engine.add(), test_engine.add(done=True)],
                    {"a": [{"answer": "ARTIFACT: data\nchrome --window-size=390 shot.png"}, {"answer": "9"}]})
        self.out = os.path.join(self.dir, "repaired")
        code, said, err = self.run_main("--lessons", self.book_path, "--repairs", "1", members=("a",))
        self.assertEqual(code, 0, said + err)
        turn = self.turns()["a"]
        self.assertEqual((turn["status"], turn["best_of"]), ("valid", 2))  # the repair is the version that counts
        self.assertEqual(len(turn["lessons_repeated"]), 1)  # but the first version made the mistake

    def test_a_lessons_file_that_cannot_be_read_stops_the_run_before_it_starts(self):
        os.makedirs(self.book_path)  # a folder, not a file
        self.out = os.path.join(self.dir, "never")
        code, said, err = self.run_main("--lessons", self.book_path)
        self.assertEqual(code, 2)
        self.assertIn("--lessons", err)
        self.assertFalse(os.path.exists(self.out))


class CommandLineTest(unittest.TestCase):
    def test_add_list_review(self):
        folder = test_engine.tempfile.mkdtemp()
        self.addCleanup(test_engine.shutil.rmtree, folder, True)
        path = os.path.join(folder, "l.jsonl")

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = lessons.main([path] + list(argv))
            return code, out.getvalue(), err.getvalue()
        code, said, _ = run("add", "--when", "screenshot", "--wrong", "w", "--right", "r", "--by", "me", "--run", folder, "--step", "t1")
        self.assertEqual(code, 0)
        self.assertRegex(said, r"^L[0-9a-f]{8} \(hypothesis\)")
        with open(path) as handle:
            self.assertEqual(json.loads(handle.readline())["source"], {"run": folder, "step": "t1"})
        self.assertEqual(run("add", "--when", "(", "--wrong", "w", "--right", "r", "--by", "me")[0], 2)
        self.assertIn("give the run folders", run("review")[2])
        code, said, _ = run("review", folder)
        self.assertEqual(code, 0)
        self.assertIn("shown 0, repeated 0; mistake before the lesson -", said)


class NotebookTest(test_notebook.Base):
    def test_a_correction_on_a_page_is_told_to_the_next_runs(self):
        self.draft()
        notebook.approve(self.page(), "tester")
        self.assertNotIn("--lessons", self.run_page(dry=True)[1])  # no lessons yet
        self.script([test_notebook.add({"text": "the answer", "for": "a", "parents": []}), test_notebook.add(done=True)],
                    {"a": [{"answer": "5"}]})
        self.assertEqual(self.run_page()[0], 0)
        with self.assertRaises(notebook.NotebookError):
            notebook.correct(self.page(), "user", "w", "r", "(", run=1)
        with self.assertRaises(notebook.NotebookError):
            notebook.correct(self.page(), "user", "w", "r", "x", run=9)
        lesson = notebook.correct(self.page(), "user", "guessed a small number", "give the highest number you can",
                                  "number", detect=r"^\s*5\s*$", run=1, step="t1")
        with open(self.nb.lessons_path()) as handle:
            kept = json.loads(handle.readline())
        self.assertEqual(kept["source"]["page"], "p1")
        self.assertEqual((kept["source"]["n"], kept["source"]["step"]), (1, "t1"))
        event = self.page().events("correct")[-1]
        self.assertEqual((event["lesson"], event["by"], event["run"]), (lesson["id"], "user", 1))
        self.assertIn("--lessons " + self.nb.lessons_path(), self.run_page(dry=True)[1])
        from herdr_py import notebookview
        self.assertIn("correction %s (run 1, t1)" % lesson["id"], notebookview.history_html(self.page(), "en"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notebook.main([self.folder, "correct", "p1", "--wrong", "w2", "--right", "r2", "--when", "number"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("without --detect it cannot be measured", out.getvalue())


if __name__ == "__main__":
    unittest.main()
